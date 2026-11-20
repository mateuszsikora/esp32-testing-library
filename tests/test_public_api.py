"""The public API is closed: every type a public method, property or attribute of an exported
class takes or returns can be imported from `esp32tl`, or from the public `esp32tl.units` and
`esp32tl.recording` modules (issue #247); and a public method that hands out a function of no
arguments says it is a `Condition` (issue #285); and every condition has the shape its kind
gives it (issue #292)."""

import ast
import contextlib
import functools
import inspect
import sys
import textwrap
import types
import typing
from collections.abc import Callable, Iterator

import esp32tl
from esp32tl import recording, units
from esp32tl.link.boardlink import Interface

# What the backend calls, not the test: on an interface, every member the link's `Interface`
# declares; elsewhere, these members of these classes. A protocol the test implements, `Model`,
# `Source`, `StreamService`, `Connection`, `DatagramService` and `TcpClient`, keeps all of its own.
BACKEND_SIDE = {
    # The network accepts a connection on a service of the package's own; a session is the
    # service's, not the test's.
    (esp32tl.Broker, "accept"),
    (esp32tl.HttpServer, "accept"),
    (esp32tl.Tls, "accept"),
    # Bumble's own attribute, which the controller narrows for mypy: the air's link stays inside
    # the air.
    (esp32tl.GuestController, "link"),
}
INTERFACE_SIDE = {name for name in vars(Interface) if not name.startswith("_")}

# The exported classes a test constructs or subclasses: their constructors are public. The backend
# constructs the others.
CONSTRUCTED = {
    esp32tl.Air,
    esp32tl.BleCentral,
    esp32tl.BlePeripheral,
    esp32tl.BluetoothDevice,
    esp32tl.Broker,
    esp32tl.CertificateAuthority,
    esp32tl.Condition,
    esp32tl.GattCharacteristic,
    esp32tl.GattService,
    esp32tl.IdleFramedModel,
    esp32tl.Model,
    esp32tl.Protocol,
    esp32tl.ReactiveModel,
    esp32tl.SppPeer,
}


def esp32tl_classes(annotation: object) -> Iterator[type]:
    if isinstance(annotation, typing.TypeVar):
        if annotation.__bound__ is not None:
            yield from esp32tl_classes(annotation.__bound__)
        return
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return
    if origin is not None:
        yield from esp32tl_classes(origin)
        for argument in typing.get_args(annotation):
            for each in argument if isinstance(argument, list) else [argument]:
                yield from esp32tl_classes(each)
    elif inspect.isclass(annotation) and annotation.__module__.startswith("esp32tl"):
        yield annotation


def assigned_attributes(cls: type) -> Iterator[tuple[str, object]]:
    """The annotations of the public attributes `cls` sets on `self`, and the classes it
    constructs into them."""
    names = vars(sys.modules[cls.__module__])
    targets: list[ast.expr]
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(cls)))):
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.annotation
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            targets, value = node.targets, node.value.func
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and not target.attr.startswith("_")
            ):
                # A call to a local function or a method names no class.
                with contextlib.suppress(NameError):
                    code = compile(ast.Expression(value), cls.__module__, "eval")
                    found = eval(code, names)
                    # A generic class constructed unannotated hides its parameters: the walk
                    # would see `Condition`, not what waiting on it returns.
                    assert isinstance(node, ast.AnnAssign) or not getattr(
                        found, "__parameters__", ()
                    ), f"annotate {cls.__qualname__}.{target.attr}, a {found.__qualname__}[...]"
                    yield target.attr, found


def backend_side(cls: type, name: str) -> bool:
    if issubclass(cls, Interface) and name in INTERFACE_SIDE:
        return True
    return any(issubclass(cls, owner) and name == member for owner, member in BACKEND_SIDE)


def public_functions(cls: type) -> Iterator[tuple[str, types.FunctionType]]:
    """The package's own functions behind the public members of `cls`, and its `__init__`:
    methods, static and class methods, and the getters of properties and cached properties."""
    for name, member in inspect.getmembers_static(cls):
        if name.startswith("_") and name != "__init__":
            continue
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        if isinstance(member, property):
            member = member.fget
        member = getattr(member, "func", member)  # a functools.cached_property
        if inspect.isfunction(member) and member.__module__.startswith("esp32tl"):
            yield name, member


def public_types(cls: type) -> set[type]:
    found: set[type] = set()
    for owner in cls.__mro__:
        if not owner.__module__.startswith("esp32tl"):
            continue
        for name, annotation in inspect.get_annotations(owner, eval_str=True).items():
            if not name.startswith("_") and not backend_side(cls, name):
                found.update(esp32tl_classes(annotation))
    for name, annotation in assigned_attributes(cls):
        if not backend_side(cls, name):
            found.update(esp32tl_classes(annotation))
    for name, function in public_functions(cls):
        if backend_side(cls, name):
            continue
        if name == "__init__" and cls not in CONSTRUCTED:
            continue
        for annotation in typing.get_type_hints(function).values():
            found.update(esp32tl_classes(annotation))
    return found


def is_public(cls: type) -> bool:
    """Exported by `esp32tl`, or defined in one of the other public modules."""
    return cls.__module__ in (units.__name__, recording.__name__) or any(
        getattr(esp32tl, name) is cls for name in esp32tl.__all__
    )


def test_every_type_a_test_meets_is_public() -> None:
    exported = [
        value for name in esp32tl.__all__ if inspect.isclass(value := getattr(esp32tl, name))
    ]
    missing: dict[str, str] = {}
    seen: set[type] = set()
    todo = [(cls, "esp32tl") for cls in exported]
    while todo:
        cls, via = todo.pop()
        if cls in seen:
            continue
        seen.add(cls)
        if not is_public(cls):
            missing[f"{cls.__module__}.{cls.__qualname__}"] = via
            continue
        todo.extend((found, cls.__qualname__) for found in public_types(cls))
    assert not missing, "export these from esp32tl: " + ", ".join(
        f"{name} (from {via})" for name, via in sorted(missing.items())
    )


def test_all_names_what_the_package_exports() -> None:
    """`__all__` names every public name of the package root but its submodules, once."""
    assert len(esp32tl.__all__) == len(set(esp32tl.__all__))
    public = {
        name
        for name, value in vars(esp32tl).items()
        if not name.startswith("_") and not inspect.ismodule(value)
    }
    assert public == set(esp32tl.__all__)


def test_a_public_method_returning_a_function_of_no_arguments_says_condition() -> None:
    """A test waits on what a public method hands out to be called with no arguments: annotated
    `Callable[[], T]`, a type checker loses the `Condition`'s description and does not know that
    `bool()` of it raises, so `def wait(c: Condition[Message | None])` refuses it."""

    def no_arguments(annotation: object) -> bool:
        """`Callable[[], T]`, alone or in a union, as `Callable[[], T] | None`."""
        origin = typing.get_origin(annotation)
        if origin is typing.Union or origin is types.UnionType:
            return any(no_arguments(each) for each in typing.get_args(annotation))
        return origin is Callable and typing.get_args(annotation)[0] == []

    callables: list[str] = []
    for name in esp32tl.__all__:
        cls = getattr(esp32tl, name)
        if not inspect.isclass(cls):
            continue
        for member_name, function in public_functions(cls):
            if member_name == "__init__":
                continue
            returned = typing.get_type_hints(function).get("return")
            if no_arguments(returned):
                callables.append(f"{cls.__qualname__}.{member_name} -> {returned}")
    assert not callables, "annotate these as returning Condition[...]: " + ", ".join(
        sorted(set(callables))
    )


# The conditions for an action's outcome: a method that does something at the call, named for it,
# and returns the condition for what came of it.
ACTIONS = {
    (esp32tl.BleCentral, "connect"),
    (esp32tl.BleCentral, "disconnect"),
    (esp32tl.BleCentral, "discover"),
    (esp32tl.BleCentral, "pair"),
    (esp32tl.BleCentral, "read"),
    (esp32tl.BleCentral, "subscribe"),
    (esp32tl.BleCentral, "write"),
    (esp32tl.BlePeripheral, "indicate"),
    (esp32tl.BlePeripheral, "notify"),
    # It connects at the call, and again after each refusal.
    (esp32tl.Internet, "accepting"),
}
# The conditions for what has happened, once and for good: named for it without `is_`, as a past
# participle; true, or its time, however long ago it happened.
HAPPENED = {
    (esp32tl.TcpConnection, "established"),
    (esp32tl.TcpConnection, "closed"),
    (esp32tl.TcpConnection, "refused"),
    (esp32tl.TcpConnection, "reset"),
    (esp32tl.TcpConnection, "timed_out"),
    (esp32tl.Tls, "handshake_started"),
    (esp32tl.Tls, "handshake_completed"),
}
# Named for what they return: the console's next line keeps `line` beside pytest-embedded's
# `expect` (#200); an HTTP exchange's `response` is the outcome of the request that made it.
NAMED_FOR_IT = {
    (esp32tl.Console, "line"),
    (esp32tl.HttpExchange, "response"),
}


def conditions() -> Iterator[tuple[type, str, str, list[str]]]:
    """Every public condition of an exported class, once, on the class that defines it: the class,
    the name, `"method"`, `"property"` or `"attribute"`, and a method's parameters."""

    def is_condition(annotation: object) -> bool:
        origin = typing.get_origin(annotation) or annotation
        return inspect.isclass(origin) and issubclass(
            origin, (esp32tl.Condition, esp32tl.LineCondition)
        )

    seen: set[tuple[type, str]] = set()
    for exported in esp32tl.__all__:
        cls = getattr(esp32tl, exported)
        if not inspect.isclass(cls) or not cls.__module__.startswith("esp32tl"):
            continue
        for owner in cls.__mro__:
            if not owner.__module__.startswith("esp32tl") or owner is object:
                continue
            found: list[tuple[str, str, object, list[str]]] = []
            for name, member in vars(owner).items():
                if name.startswith("_"):
                    continue
                kind = (
                    "property"
                    if isinstance(member, (property, functools.cached_property))
                    else "method"
                )
                if isinstance(member, (staticmethod, classmethod)):
                    member = member.__func__
                if isinstance(member, property):
                    member = member.fget
                member = getattr(member, "func", member)  # a functools.cached_property
                if inspect.isfunction(member):
                    parameters = list(inspect.signature(member).parameters)[1:]
                    returned = typing.get_type_hints(member).get("return")
                    found.append((name, kind, returned, parameters))
            for name, annotation in inspect.get_annotations(owner, eval_str=True).items():
                if not name.startswith("_"):
                    found.append((name, "attribute", annotation, []))
            for name, annotation in assigned_attributes(owner):
                found.append((name, "attribute", annotation, []))
            for name, kind, annotation, parameters in found:
                if is_condition(annotation) and (owner, name) not in seen:
                    seen.add((owner, name))
                    yield owner, name, kind, parameters


def test_every_condition_has_the_shape_of_its_kind() -> None:
    """A condition for the next event is a `next_*` method, as the call is where it starts
    looking; one for an action's outcome is the action's method; one for a state is named
    `is_<state>` when it holds now and can stop holding, and for what has happened without
    `is_`; and a state condition is a property with no argument, a method with one
    (docs/guides/writing-tests.md#which-shape-a-condition-has)."""
    found = list(conditions())
    listed = ACTIONS | HAPPENED | NAMED_FOR_IT
    stale = listed - {(cls, name) for cls, name, _, _ in found}
    assert not stale, "not conditions: " + ", ".join(
        sorted(f"{cls.__qualname__}.{name}" for cls, name in stale)
    )
    wrong: list[str] = []
    for cls, name, kind, parameters in found:
        where = f"{cls.__qualname__}.{name}, a {kind}"
        if name.startswith("next_"):
            if kind != "method":
                wrong.append(f"{where}: a next_* condition is a method")
        elif (cls, name) in ACTIONS:
            if kind != "method":
                wrong.append(f"{where}: an action is a method")
        elif (cls, name) in NAMED_FOR_IT:
            continue
        else:
            if parameters and kind != "method":
                wrong.append(f"{where}: a state condition with an argument is a method")
            if not parameters and kind == "method":
                wrong.append(f"{where}: a state condition with no argument is a property")
            if (cls, name) in HAPPENED and name.startswith("is_"):
                wrong.append(f"{where}: what has happened is named without is_")
            if (cls, name) not in HAPPENED and not name.startswith("is_"):
                wrong.append(
                    f"{where}: a state that holds now is named is_<state>; what has happened"
                    " once and for good goes in HAPPENED, an action in ACTIONS"
                )
    assert not wrong, "\n".join(sorted(wrong))
