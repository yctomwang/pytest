import abc
import os
import warnings
from functools import cached_property
from inspect import signature
from pathlib import Path
from typing import Any
from typing import Callable
from typing import cast
from typing import Iterable
from typing import Iterator
from typing import List
from typing import MutableMapping
from typing import Optional
from typing import overload
from typing import Set
from typing import Tuple
from typing import Type
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import Union

import pluggy

import _pytest._code
from _pytest._code import getfslineno
from _pytest._code.code import ExceptionInfo
from _pytest._code.code import TerminalRepr
from _pytest._code.code import Traceback
from _pytest.config import Config
from _pytest.config import ConftestImportFailure
from _pytest.mark.structures import Mark
from _pytest.mark.structures import MarkDecorator
from _pytest.mark.structures import NodeKeywords
from _pytest.outcomes import fail
from _pytest.pathlib import absolutepath
from _pytest.pathlib import commonpath
from _pytest.stash import Stash
from _pytest.warning_types import PytestWarning

if TYPE_CHECKING:
    # Imported here due to circular import.
    from _pytest.main import Session
    from _pytest._code.code import _TracebackStyle


SEP = "/"

tracebackcutdir = Path(_pytest.__file__).parent


_NodeType = TypeVar("_NodeType", bound="Node")


class NodeMeta(abc.ABCMeta):
    """Metaclass used by :class:`Node` to enforce that direct construction raises
    :class:`Failed`.

    This behaviour supports the indirection introduced with :meth:`Node.from_parent`,
    the named constructor to be used instead of direct construction. The design
    decision to enforce indirection with :class:`NodeMeta` was made as a
    temporary aid for refactoring the collection tree, which was diagnosed to
    have :class:`Node` objects whose creational patterns were overly entangled.
    Once the refactoring is complete, this metaclass can be removed.

    See https://github.com/pytest-dev/pytest/projects/3 for an overview of the
    progress on detangling the :class:`Node` classes.
    """

    def __call__(self, *k, **kw):
        msg = (
            "Direct construction of {name} has been deprecated, please use {name}.from_parent.\n"
            "See "
            "https://docs.pytest.org/en/stable/deprecations.html#node-construction-changed-to-node-from-parent"
            " for more details."
        ).format(name=f"{self.__module__}.{self.__name__}")
        fail(msg, pytrace=False)

    def _create(self, *k, **kw):
        try:
            return super().__call__(*k, **kw)
        except TypeError:
            sig = signature(getattr(self, "__init__"))
            known_kw = {k: v for k, v in kw.items() if k in sig.parameters}
            from .warning_types import PytestDeprecationWarning

            warnings.warn(
                PytestDeprecationWarning(
                    f"{self} is not using a cooperative constructor and only takes {set(known_kw)}.\n"
                    "See https://docs.pytest.org/en/stable/deprecations.html"
                    "#constructors-of-custom-pytest-node-subclasses-should-take-kwargs "
                    "for more details."
                )
            )

            return super().__call__(*k, **known_kw)


class Node(abc.ABC, metaclass=NodeMeta):
    r"""Base class of :class:`Collector` and :class:`Item`, the components of
    the test collection tree.

    ``Collector``\'s are the internal nodes of the tree, and ``Item``\'s are the
    leaf nodes.
    """
    # Use __slots__ to make attribute access faster.
    # Note that __dict__ is still available.
    __slots__ = (
        "name",
        "parent",
        "config",
        "session",
        "path",
        "_nodeid",
        "_store",
        "__dict__",
    )

    def __init__(
        self,
        name: str,
        parent: "Optional[Node]" = None,
        config: Optional[Config] = None,
        session: "Optional[Session]" = None,
        path: Optional[Path] = None,
        nodeid: Optional[str] = None,
    ) -> None:
        #: A unique name within the scope of the parent node.
        self.name: str = name

        #: The parent collector node.
        self.parent = parent

        if config:
            #: The pytest config object.
            self.config: Config = config
        else:
            if not parent:
                raise TypeError("config or parent must be provided")
            self.config = parent.config

        if session:
            #: The pytest session this node is part of.
            self.session: Session = session
        else:
            if not parent:
                raise TypeError("session or parent must be provided")
            self.session = parent.session

        if path is None:
            path = getattr(parent, "path", None)
        assert path is not None
        #: Filesystem path where this node was collected from (can be None).
        self.path = path

        # The explicit annotation is to avoid publicly exposing NodeKeywords.
        #: Keywords/markers collected from all scopes.
        self.keywords: MutableMapping[str, Any] = NodeKeywords(self)

        #: The marker objects belonging to this node.
        self.own_markers: List[Mark] = []

        #: Allow adding of extra keywords to use for matching.
        self.extra_keyword_matches: Set[str] = set()

        if nodeid is not None:
            assert "::()" not in nodeid
            self._nodeid = nodeid
        else:
            if not self.parent:
                raise TypeError("nodeid or parent must be provided")
            self._nodeid = self.parent.nodeid + "::" + self.name

        #: A place where plugins can store information on the node for their
        #: own use.
        self.stash: Stash = Stash()
        # Deprecated alias. Was never public. Can be removed in a few releases.
        self._store = self.stash

    @classmethod
    def from_parent(cls, parent: "Node", **kw):
        """Public constructor for Nodes.

        This indirection got introduced in order to enable removing
        the fragile logic from the node constructors.

        Subclasses can use ``super().from_parent(...)`` when overriding the
        construction.

        :param parent: The parent node of this Node.
        """
        if "config" in kw:
            raise TypeError("config is not a valid argument for from_parent")
        if "session" in kw:
            raise TypeError("session is not a valid argument for from_parent")
        return cls._create(parent=parent, **kw)

    @property
    def ihook(self) -> pluggy.HookRelay:
        """fspath-sensitive hook proxy used to call pytest hooks."""
        return self.session.gethookproxy(self.path)

    def __repr__(self) -> str:
        return "<{} {}>".format(self.__class__.__name__, getattr(self, "name", None))

    def warn(self, warning: Warning) -> None:
        """Issue a warning for this Node.

        Warnings will be displayed after the test session, unless explicitly suppressed.

        :param Warning warning:
            The warning instance to issue.

        :raises ValueError: If ``warning`` instance is not a subclass of Warning.

        Example usage:

        .. code-block:: python

            node.warn(PytestWarning("some message"))
            node.warn(UserWarning("some message"))

        .. versionchanged:: 6.2
            Any subclass of :class:`Warning` is now accepted, rather than only
            :class:`PytestWarning <pytest.PytestWarning>` subclasses.
        """
        # enforce type checks here to avoid getting a generic type error later otherwise.
        if not isinstance(warning, Warning):
            raise ValueError(
                "warning must be an instance of Warning or subclass, got {!r}".format(
                    warning
                )
            )
        path, lineno = get_fslocation_from_item(self)
        assert lineno is not None
        warnings.warn_explicit(
            warning,
            category=None,
            filename=str(path),
            lineno=lineno + 1,
        )

    # Methods for ordering nodes.

    @property
    def nodeid(self) -> str:
        """A ::-separated string denoting its collection tree address."""
        return self._nodeid

    def __hash__(self) -> int:
        return hash(self._nodeid)

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def iterparents(self) -> Iterator["Node"]:
        """Iterate over all parent collectors starting from and including self
        up to the root of the collection tree.

        .. versionadded:: 8.1
        """
        parent: Optional[Node] = self
        while parent is not None:
            yield parent
            parent = parent.parent

    def listchain(self) -> List["Node"]:
        """Return a list of all parent collectors starting from the root of the
        collection tree down to and including self."""
        chain = []
        item: Optional[Node] = self
        while item is not None:
            chain.append(item)
            item = item.parent
        chain.reverse()
        return chain

    def add_marker(
        self, marker: Union[str, MarkDecorator], append: bool = True
    ) -> None:
        """Dynamically add a marker object to the node.

        :param marker:
            The marker.
        :param append:
            Whether to append the marker, or prepend it.
        """
        from _pytest.mark import MARK_GEN

        if isinstance(marker, MarkDecorator):
            marker_ = marker
        elif isinstance(marker, str):
            marker_ = getattr(MARK_GEN, marker)
        else:
            raise ValueError("is not a string or pytest.mark.* Marker")
        self.keywords[marker_.name] = marker_
        if append:
            self.own_markers.append(marker_.mark)
        else:
            self.own_markers.insert(0, marker_.mark)

    def iter_markers(self, name: Optional[str] = None) -> Iterator[Mark]:
        """Iterate over all markers of the node.

        :param name: If given, filter the results by the name attribute.
        :returns: An iterator of the markers of the node.
        """
        return (x[1] for x in self.iter_markers_with_node(name=name))

    def iter_markers_with_node(
        self, name: Optional[str] = None
    ) -> Iterator[Tuple["Node", Mark]]:
        """Iterate over all markers of the node.

        :param name: If given, filter the results by the name attribute.
        :returns: An iterator of (node, mark) tuples.
        """
        for node in self.iterparents():
            for mark in node.own_markers:
                if name is None or getattr(mark, "name", None) == name:
                    yield node, mark

    @overload
    def get_closest_marker(self, name: str) -> Optional[Mark]:
        ...

    @overload
    def get_closest_marker(self, name: str, default: Mark) -> Mark:
        ...

    def get_closest_marker(
        self, name: str, default: Optional[Mark] = None
    ) -> Optional[Mark]:
        """Return the first marker matching the name, from closest (for
        example function) to farther level (for example module level).

        :param default: Fallback return value if no marker was found.
        :param name: Name to filter by.
        """
        return next(self.iter_markers(name=name), default)

    def listextrakeywords(self) -> Set[str]:
        """Return a set of all extra keywords in self and any parents."""
        extra_keywords: Set[str] = set()
        for item in self.listchain():
            extra_keywords.update(item.extra_keyword_matches)
        return extra_keywords

    def listnames(self) -> List[str]:
        return [x.name for x in self.listchain()]

    def addfinalizer(self, fin: Callable[[], object]) -> None:
        """Register a function to be called without arguments when this node is
        finalized.

        This method can only be called when this node is active
        in a setup chain, for example during self.setup().
        """
        self.session._setupstate.addfinalizer(fin, self)

    def getparent(self, cls: Type[_NodeType]) -> Optional[_NodeType]:
        """Get the closest parent node (including self) which is an instance of
        the given class.

        :param cls: The node class to search for.
        :returns: The node, if found.
        """
        for node in self.iterparents():
            if isinstance(node, cls):
                return node
        return None

    def _traceback_filter(self, excinfo: ExceptionInfo[BaseException]) -> Traceback:
        return excinfo.traceback

    def _repr_failure_py(
        self,
        excinfo: ExceptionInfo[BaseException],
        style: "Optional[_TracebackStyle]" = None,
    ) -> TerminalRepr:
        from _pytest.fixtures import FixtureLookupError

        if isinstance(excinfo.value, ConftestImportFailure):
            excinfo = ExceptionInfo.from_exc_info(excinfo.value.excinfo)
        if isinstance(excinfo.value, fail.Exception):
            if not excinfo.value.pytrace:
                style = "value"
        if isinstance(excinfo.value, FixtureLookupError):
            return excinfo.value.formatrepr()

        tbfilter: Union[bool, Callable[[ExceptionInfo[BaseException]], Traceback]]
        if self.config.getoption("fulltrace", False):
            style = "long"
            tbfilter = False
        else:
            tbfilter = self._traceback_filter
            if style == "auto":
                style = "long"
        # XXX should excinfo.getrepr record all data and toterminal() process it?
        if style is None:
            if self.config.getoption("tbstyle", "auto") == "short":
                style = "short"
            else:
                style = "long"

        if self.config.getoption("verbose", 0) > 1:
            truncate_locals = False
        else:
            truncate_locals = True

        # excinfo.getrepr() formats paths relative to the CWD if `abspath` is False.
        # It is possible for a fixture/test to change the CWD while this code runs, which
        # would then result in the user seeing confusing paths in the failure message.
        # To fix this, if the CWD changed, always display the full absolute path.
        # It will be better to just always display paths relative to invocation_dir, but
        # this requires a lot of plumbing (#6428).
        try:
            abspath = Path(os.getcwd()) != self.config.invocation_params.dir
        except OSError:
            abspath = True

        return excinfo.getrepr(
            funcargs=True,
            abspath=abspath,
            showlocals=self.config.getoption("showlocals", False),
            style=style,
            tbfilter=tbfilter,
            truncate_locals=truncate_locals,
        )

    def repr_failure(
        self,
        excinfo: ExceptionInfo[BaseException],
        style: "Optional[_TracebackStyle]" = None,
    ) -> Union[str, TerminalRepr]:
        """Return a representation of a collection or test failure.

        .. seealso:: :ref:`non-python tests`

        :param excinfo: Exception information for the failure.
        """
        return self._repr_failure_py(excinfo, style)


def get_fslocation_from_item(node: "Node") -> Tuple[Union[str, Path], Optional[int]]:
    """Try to extract the actual location from a node, depending on available attributes:

    * "location": a pair (path, lineno)
    * "obj": a Python object that the node wraps.
    * "path": just a path

    :rtype: A tuple of (str|Path, int) with filename and 0-based line number.
    """
    # See Item.location.
    location: Optional[Tuple[str, Optional[int], str]] = getattr(node, "location", None)
    if location is not None:
        return location[:2]
    obj = getattr(node, "obj", None)
    if obj is not None:
        return getfslineno(obj)
    return getattr(node, "path", "unknown location"), -1


class Collector(Node, abc.ABC):
    """Base class of all collectors.

    Collector create children through `collect()` and thus iteratively build
    the collection tree.
    """

    class CollectError(Exception):
        """An error during collection, contains a custom message."""

    @abc.abstractmethod
    def collect(self) -> Iterable[Union["Item", "Collector"]]:
        """Collect children (items and collectors) for this collector."""
        raise NotImplementedError("abstract")

    # TODO: This omits the style= parameter which breaks Liskov Substitution.
    def repr_failure(  # type: ignore[override]
        self, excinfo: ExceptionInfo[BaseException]
    ) -> Union[str, TerminalRepr]:
        """Return a representation of a collection failure.

        :param excinfo: Exception information for the failure.
        """
        if isinstance(excinfo.value, self.CollectError) and not self.config.getoption(
            "fulltrace", False
        ):
            exc = excinfo.value
            return str(exc.args[0])

        # Respect explicit tbstyle option, but default to "short"
        # (_repr_failure_py uses "long" with "fulltrace" option always).
        tbstyle = self.config.getoption("tbstyle", "auto")
        if tbstyle == "auto":
            tbstyle = "short"

        return self._repr_failure_py(excinfo, style=tbstyle)

    def _traceback_filter(self, excinfo: ExceptionInfo[BaseException]) -> Traceback:
        if hasattr(self, "path"):
            traceback = excinfo.traceback
            ntraceback = traceback.cut(path=self.path)
            if ntraceback == traceback:
                ntraceback = ntraceback.cut(excludepath=tracebackcutdir)
            return ntraceback.filter(excinfo)
        return excinfo.traceback


def _check_initialpaths_for_relpath(session: "Session", path: Path) -> Optional[str]:
    for initial_path in session._initialpaths:
        if commonpath(path, initial_path) == initial_path:
            rel = str(path.relative_to(initial_path))
            return "" if rel == "." else rel
    return None


class FSCollector(Collector, abc.ABC):
    """Base class for filesystem collectors."""

    def __init__(
        self,
        path_or_parent: Optional[Union[Path, Node]] = None,
        path: Optional[Path] = None,
        name: Optional[str] = None,
        parent: Optional[Node] = None,
        config: Optional[Config] = None,
        session: Optional["Session"] = None,
        nodeid: Optional[str] = None,
    ) -> None:
        if path_or_parent:
            if isinstance(path_or_parent, Node):
                assert parent is None
                parent = cast(FSCollector, path_or_parent)
            elif isinstance(path_or_parent, Path):
                assert path is None
                path = path_or_parent
        assert path is not None

        if name is None:
            name = path.name
            if parent is not None and parent.path != path:
                try:
                    rel = path.relative_to(parent.path)
                except ValueError:
                    pass
                else:
                    name = str(rel)
                name = name.replace(os.sep, SEP)
        self.path = path

        if session is None:
            assert parent is not None
            session = parent.session

        if nodeid is None:
            try:
                nodeid = str(self.path.relative_to(session.config.rootpath))
            except ValueError:
                nodeid = _check_initialpaths_for_relpath(session, path)

            if nodeid and os.sep != SEP:
                nodeid = nodeid.replace(os.sep, SEP)

        super().__init__(
            name=name,
            parent=parent,
            config=config,
            session=session,
            nodeid=nodeid,
            path=path,
        )

    @classmethod
    def from_parent(
        cls,
        parent,
        *,
        path: Optional[Path] = None,
        **kw,
    ):
        """The public constructor."""
        return super().from_parent(parent=parent, path=path, **kw)


class File(FSCollector, abc.ABC):
    """Base class for collecting tests from a file.

    :ref:`non-python tests`.
    """


class Directory(FSCollector, abc.ABC):
    """Base class for collecting files from a directory.

    A basic directory collector does the following: goes over the files and
    sub-directories in the directory and creates collectors for them by calling
    the hooks :hook:`pytest_collect_directory` and :hook:`pytest_collect_file`,
    after checking that they are not ignored using
    :hook:`pytest_ignore_collect`.

    The default directory collectors are :class:`~pytest.Dir` and
    :class:`~pytest.Package`.

    .. versionadded:: 8.0

    :ref:`custom directory collectors`.
    """


class Item(Node, abc.ABC):
    """Base class of all test invocation items.

    Note that for a single function there might be multiple test invocation items.
    """

    nextitem = None

    def __init__(
        self,
        name,
        parent=None,
        config: Optional[Config] = None,
        session: Optional["Session"] = None,
        nodeid: Optional[str] = None,
        **kw,
    ) -> None:
        # The first two arguments are intentionally passed positionally,
        # to keep plugins who define a node type which inherits from
        # (pytest.Item, pytest.File) working (see issue #8435).
        # They can be made kwargs when the deprecation above is done.
        super().__init__(
            name,
            parent,
            config=config,
            session=session,
            nodeid=nodeid,
            **kw,
        )
        self._report_sections: List[Tuple[str, str, str]] = []

        #: A list of tuples (name, value) that holds user defined properties
        #: for this test.
        self.user_properties: List[Tuple[str, object]] = []

        self._check_item_and_collector_diamond_inheritance()

    def _check_item_and_collector_diamond_inheritance(self) -> None:
        """
        Check if the current type inherits from both File and Collector
        at the same time, emitting a warning accordingly (#8447).
        """
        cls = type(self)

        # We inject an attribute in the type to avoid issuing this warning
        # for the same class more than once, which is not helpful.
        # It is a hack, but was deemed acceptable in order to avoid
        # flooding the user in the common case.
        attr_name = "_pytest_diamond_inheritance_warning_shown"
        if getattr(cls, attr_name, False):
            return
        setattr(cls, attr_name, True)

        problems = ", ".join(
            base.__name__ for base in cls.__bases__ if issubclass(base, Collector)
        )
        if problems:
            warnings.warn(
                f"{cls.__name__} is an Item subclass and should not be a collector, "
                f"however its bases {problems} are collectors.\n"
                "Please split the Collectors and the Item into separate node types.\n"
                "Pytest Doc example: https://docs.pytest.org/en/latest/example/nonpython.html\n"
                "example pull request on a plugin: https://github.com/asmeurer/pytest-flakes/pull/40/",
                PytestWarning,
            )

    @abc.abstractmethod
    def runtest(self) -> None:
        """Run the test case for this item.

        Must be implemented by subclasses.

        .. seealso:: :ref:`non-python tests`
        """
        raise NotImplementedError("runtest must be implemented by Item subclass")

    def add_report_section(self, when: str, key: str, content: str) -> None:
        """Add a new report section, similar to what's done internally to add
        stdout and stderr captured output::

            item.add_report_section("call", "stdout", "report section contents")

        :param str when:
            One of the possible capture states, ``"setup"``, ``"call"``, ``"teardown"``.
        :param str key:
            Name of the section, can be customized at will. Pytest uses ``"stdout"`` and
            ``"stderr"`` internally.
        :param str content:
            The full contents as a string.
        """
        if content:
            self._report_sections.append((when, key, content))

    def reportinfo(self) -> Tuple[Union["os.PathLike[str]", str], Optional[int], str]:
        """Get location information for this item for test reports.

        Returns a tuple with three elements:

        - The path of the test (default ``self.path``)
        - The 0-based line number of the test (default ``None``)
        - A name of the test to be shown (default ``""``)

        .. seealso:: :ref:`non-python tests`
        """
        return self.path, None, ""

    @cached_property
    def location(self) -> Tuple[str, Optional[int], str]:
        """
        Returns a tuple of ``(relfspath, lineno, testname)`` for this item
        where ``relfspath`` is file path relative to ``config.rootpath``
        and lineno is a 0-based line number.
        """
        location = self.reportinfo()
        path = absolutepath(os.fspath(location[0]))
        relfspath = self.session._node_location_to_relpath(path)
        assert type(location[2]) is str
        return (relfspath, location[1], location[2])
