"""The public API is closed: every type a public method, property or attribute of an exported
class takes or returns can be imported from `esp32tl`, or from the public `esp32tl.units` and
`esp32tl.recording` modules (issue #247)."""

import ast
import contextlib
import inspect
import sys
import textwrap
import typing
from collections.abc import Iterator

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
    esp32tl.BluetoothDevice,
    esp32tl.Broker,
    esp32tl.CertificateAuthority,
    esp32tl.Condition,
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
    for name, member in inspect.getmembers_static(cls):
        if name.startswith("_") and name != "__init__":
            continue
        if backend_side(cls, name):
            continue
        if name == "__init__" and cls not in CONSTRUCTED:
            continue
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        if isinstance(member, property):
            member = member.fget
        member = getattr(member, "func", member)  # a functools.cached_property
        if inspect.isfunction(member) and member.__module__.startswith("esp32tl"):
            for annotation in typing.get_type_hints(member).values():
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
