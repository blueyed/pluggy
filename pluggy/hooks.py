"""
Internal hook annotation, representation and calling machinery.
"""
import inspect
import warnings
from .callers import _legacymulticall, _multicall


class HookspecMarker(object):
    """ Decorator helper class for marking functions as hook specifications.

    You can instantiate it with a project_name to get a decorator.
    Calling PluginManager.add_hookspecs later will discover all marked functions
    if the PluginManager uses the same project_name.
    """

    def __init__(self, project_name):
        self.project_name = project_name

    def __call__(self, function=None, firstresult=False, historic=False, warn_on_impl=None):
        """ if passed a function, directly sets attributes on the function
        which will make it discoverable to add_hookspecs().  If passed no
        function, returns a decorator which can be applied to a function
        later using the attributes supplied.

        If firstresult is True the 1:N hook call (N being the number of registered
        hook implementation functions) will stop at I<=N when the I'th function
        returns a non-None result.

        If historic is True calls to a hook will be memorized and replayed
        on later registered plugins.

        """
        def setattr_hookspec_opts(func):
            if historic and firstresult:
                raise ValueError("cannot have a historic firstresult hook")
            setattr(func, self.project_name + "_spec",
                    dict(firstresult=firstresult, historic=historic,
                         warn_on_impl=warn_on_impl,))
            return func

        if function is not None:
            return setattr_hookspec_opts(function)
        else:
            return setattr_hookspec_opts


class HookimplMarker(object):
    """ Decorator helper class for marking functions as hook implementations.

    You can instantiate with a project_name to get a decorator.
    Calling PluginManager.register later will discover all marked functions
    if the PluginManager uses the same project_name.
    """
    def __init__(self, project_name):
        self.project_name = project_name

    def __call__(self, function=None, hookwrapper=False, optionalhook=False,
                 tryfirst=False, trylast=False):

        """ if passed a function, directly sets attributes on the function
        which will make it discoverable to register().  If passed no function,
        returns a decorator which can be applied to a function later using
        the attributes supplied.

        If optionalhook is True a missing matching hook specification will not result
        in an error (by default it is an error if no matching spec is found).

        If tryfirst is True this hook implementation will run as early as possible
        in the chain of N hook implementations for a specfication.

        If trylast is True this hook implementation will run as late as possible
        in the chain of N hook implementations.

        If hookwrapper is True the hook implementations needs to execute exactly
        one "yield".  The code before the yield is run early before any non-hookwrapper
        function is run.  The code after the yield is run after all non-hookwrapper
        function have run.  The yield receives a ``_Result`` object representing
        the exception or result outcome of the inner calls (including other hookwrapper
        calls).

        """
        def setattr_hookimpl_opts(func):
            setattr(func, self.project_name + "_impl",
                    dict(hookwrapper=hookwrapper, optionalhook=optionalhook,
                         tryfirst=tryfirst, trylast=trylast))
            return func

        if function is None:
            return setattr_hookimpl_opts
        else:
            return setattr_hookimpl_opts(function)


def normalize_hookimpl_opts(opts):
    opts.setdefault("tryfirst", False)
    opts.setdefault("trylast", False)
    opts.setdefault("hookwrapper", False)
    opts.setdefault("optionalhook", False)


if hasattr(inspect, 'getfullargspec'):
    def _getargspec(func):
        return inspect.getfullargspec(func)
else:
    def _getargspec(func):
        return inspect.getargspec(func)


def varnames(func):
    """Return tuple of positional and keywrord argument names for a function,
    method, class or callable.

    In case of a class, its ``__init__`` method is considered.
    For methods the ``self`` parameter is not included.
    """
    cache = getattr(func, "__dict__", {})
    try:
        return cache["_varnames"]
    except KeyError:
        pass

    if inspect.isclass(func):
        try:
            func = func.__init__
        except AttributeError:
            return (), ()
    elif not inspect.isroutine(func):  # callable object?
        try:
            func = getattr(func, '__call__', func)
        except Exception:
            return ()

    try:  # func MUST be a function or method here or we won't parse any args
        spec = _getargspec(func)
    except TypeError:
        return (), ()

    args, defaults = tuple(spec.args), spec.defaults
    if defaults:
        index = -len(defaults)
        args, defaults = args[:index], tuple(args[index:])
    else:
        defaults = ()

    # strip any implicit instance arg
    if args:
        if inspect.ismethod(func) or (
            '.' in getattr(func, '__qualname__', ()) and args[0] == 'self'
        ):
            args = args[1:]

    assert "self" not in args  # best naming practises check?
    try:
        cache["_varnames"] = args, defaults
    except TypeError:
        pass
    return args, defaults


class _HookRelay(object):
    """ hook holder object for performing 1:N hook calls where N is the number
    of registered plugins.

    """

    def __init__(self, trace):
        self._trace = trace


class _HookCaller(object):
    def __init__(self, name, hook_execute, specmodule_or_class=None,
                 spec_opts=None):
        self.name = name
        self._wrappers = []
        self._nonwrappers = []
        self._hookexec = hook_execute
        self._specmodule_or_class = None
        self.argnames = None
        self.kwargnames = None
        self.multicall = _multicall
        self.spec_opts = spec_opts or {}
        if specmodule_or_class is not None:
            self.set_specification(specmodule_or_class, spec_opts)

    def has_spec(self):
        return self._specmodule_or_class is not None

    def set_specification(self, specmodule_or_class, spec_opts):
        assert not self.has_spec()
        self._specmodule_or_class = specmodule_or_class
        specfunc = getattr(specmodule_or_class, self.name)
        # get spec arg signature
        argnames, self.kwargnames = varnames(specfunc)
        self.argnames = ["__multicall__"] + list(argnames)
        self.spec_opts.update(spec_opts)
        if spec_opts.get("historic"):
            self._call_history = []
        self.warn_on_impl = spec_opts.get('warn_on_impl')

    def is_historic(self):
        return hasattr(self, "_call_history")

    def _remove_plugin(self, plugin):
        def remove(wrappers):
            for i, method in enumerate(wrappers):
                if method.plugin == plugin:
                    del wrappers[i]
                    return True
        if remove(self._wrappers) is None:
            if remove(self._nonwrappers) is None:
                raise ValueError("plugin %r not found" % (plugin,))

    def _add_hookimpl(self, hookimpl):
        """Add an implementation to the callback chain.
        """
        if hookimpl.hookwrapper:
            methods = self._wrappers
        else:
            methods = self._nonwrappers

        if hookimpl.trylast:
            methods.insert(0, hookimpl)
        elif hookimpl.tryfirst:
            methods.append(hookimpl)
        else:
            # find last non-tryfirst method
            i = len(methods) - 1
            while i >= 0 and methods[i].tryfirst:
                i -= 1
            methods.insert(i + 1, hookimpl)

        if '__multicall__' in hookimpl.argnames:
            warnings.warn(
                "Support for __multicall__ is now deprecated and will be"
                "removed in an upcoming release.",
                DeprecationWarning
            )
            self.multicall = _legacymulticall

    def __repr__(self):
        return "<_HookCaller %r>" % (self.name,)

    def __call__(self, *args, **kwargs):
        if args:
            raise TypeError("hook calling supports only keyword arguments")
        assert not self.is_historic()
        if self.argnames:
            notincall = set(self.argnames) - set(['__multicall__']) - set(
                kwargs.keys())
            if notincall:
                warnings.warn(
                    "Argument(s) {} which are declared in the hookspec "
                    "can not be found in this hook call"
                    .format(tuple(notincall)),
                    stacklevel=2,
                )
        return self._hookexec(self, self._nonwrappers + self._wrappers, kwargs)

    def call_historic(self, proc=None, kwargs=None):
        """ call the hook with given ``kwargs`` for all registered plugins and
        for all plugins which will be registered afterwards.

        If ``proc`` is not None it will be called for for each non-None result
        obtained from a hook implementation.
        """
        self._call_history.append((kwargs or {}, proc))
        # historizing hooks don't return results
        res = self._hookexec(self, self._nonwrappers + self._wrappers, kwargs)
        if proc is None:
            return
        for x in res or []:
            proc(x)

    def call_extra(self, methods, kwargs):
        """ Call the hook with some additional temporarily participating
        methods using the specified kwargs as call parameters. """
        old = list(self._nonwrappers), list(self._wrappers)
        for method in methods:
            opts = dict(hookwrapper=False, trylast=False, tryfirst=False)
            hookimpl = HookImpl(None, "<temp>", method, opts)
            self._add_hookimpl(hookimpl)
        try:
            return self(**kwargs)
        finally:
            self._nonwrappers, self._wrappers = old

    def _maybe_apply_history(self, method):
        """Apply call history to a new hookimpl if it is marked as historic.
        """
        if self.is_historic():
            for kwargs, proc in self._call_history:
                res = self._hookexec(self, [method], kwargs)
                if res and proc is not None:
                    proc(res[0])


class HookImpl(object):
    def __init__(self, plugin, plugin_name, function, hook_impl_opts):
        self.function = function
        self.argnames, self.kwargnames = varnames(self.function)
        self.plugin = plugin
        self.opts = hook_impl_opts
        self.plugin_name = plugin_name
        self.__dict__.update(hook_impl_opts)
