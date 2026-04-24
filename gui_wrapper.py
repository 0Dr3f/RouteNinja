"""RouteNinja UI wiring: plugin commands + sidebar widget.

Mirrors the pattern used by EmuNinja. GUIWrapper is the orchestrator — the
sidebar widget gets a reference to the shared RouteNinja instance so any
right-click action and any sidebar button mutate the same state.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import List, Optional

from PySide6 import QtCore
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from binaryninja import BinaryView, Function, PluginCommand, interaction
from binaryninjaui import (
    Sidebar,
    SidebarContextSensitivity,
    SidebarWidget,
    SidebarWidgetLocation,
    SidebarWidgetType,
    UIActionHandler,
)

from RouteNinja.graph_view import show_call_tree, show_paths
from RouteNinja.route_ninja import AnnotatedPath, RouteNinja


# Fallback icon (base64 PNG) — used only when logo.png is missing or fails
# to decode. Normal path loads logo.png from the plugin directory.
_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
_FALLBACK_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAADgAAAA4CAYAAACohjseAAAAbklEQVR4nO3VQQrAIAwF"
    "0f+f2sbt4DqgtCBhZhVwjhKRSkmSJEnP87zufpO2KvRAq6O5qTsHqjfalVFuVu8U8a6c"
    "uvV3rn5u91X1tu63iHfpVK3fEe8qZ62/dfX+qt5FvLO8tf6qeqe8W0mSJElS54Pp4Aom"
    "jRq7eQAAAABJRU5ErkJggg=="
)


def _load_sidebar_icon() -> QImage:
    """Load logo.png from the plugin directory. Fall back to the embedded
    placeholder if the file is missing or unreadable so the sidebar icon
    always renders something — a missing icon silently breaks the sidebar
    entry on some BN versions."""
    try:
        if os.path.isfile(_ICON_PATH):
            img = QImage(_ICON_PATH)
            if not img.isNull():
                return img
            logging.warning(f"RouteNinja: logo.png at {_ICON_PATH} did not decode.")
    except Exception:
        logging.exception("RouteNinja: failed loading logo.png")
    img = QImage.fromData(base64.b64decode(_FALLBACK_ICON_B64))
    if img.isNull():
        img = QImage(56, 56, QImage.Format_RGB32)
        img.fill(0)
    return img


def _coerce_int(text: str, base: int = 0) -> Optional[int]:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return int(text, base)
    except ValueError:
        return None


def _resolve_address_to_function(bv: BinaryView, addr_or_name: str) -> Optional[Function]:
    """Accept either a hex/decimal address or a function name. Return a
    matching Function or None."""
    if bv is None:
        return None
    candidate = addr_or_name.strip()
    if not candidate:
        return None

    # Numeric first
    addr = _coerce_int(candidate, base=0)
    if addr is not None:
        fns = list(bv.get_functions_at(addr))
        if fns:
            return fns[0]
        fns = list(bv.get_functions_containing(addr))
        if fns:
            return fns[0]

    # Name lookup
    try:
        fns = list(bv.get_functions_by_name(candidate))
    except Exception:
        fns = []
    if fns:
        return fns[0]
    return None


class GUIWrapper:
    """Wires PluginCommand registrations and the sidebar widget."""

    def __init__(self, pt: RouteNinja):
        self.pt = pt

    # ---- registration --------------------------------------------------------

    def register_sidebar_widget(self) -> None:
        Sidebar.addSidebarWidgetType(RouteNinjaSidebarType(self.pt))

    def register_plugin_commands(self) -> None:
        PluginCommand.register_for_function(
            "RouteNinja\\Set as Source",
            "Mark this function as the path search source.",
            self.cmd_set_source_function,
        )
        PluginCommand.register_for_function(
            "RouteNinja\\Set as Target",
            "Mark this function as the path search target.",
            self.cmd_set_target_function,
        )
        PluginCommand.register_for_function(
            "RouteNinja\\Find Paths To This Function",
            "Find paths from the configured source to this function.",
            self.cmd_find_paths_to_function,
        )
        PluginCommand.register_for_function(
            "RouteNinja\\Find Paths From This Function",
            "Find paths from this function to the configured target.",
            self.cmd_find_paths_from_function,
        )
        PluginCommand.register(
            "RouteNinja\\Find Paths (configured endpoints)",
            "Find paths between the configured source and target functions.",
            self.cmd_find_paths,
        )
        PluginCommand.register(
            "RouteNinja\\Show Last Paths Graph",
            "Display the most recent RouteNinja result as a flow graph.",
            self.cmd_show_last_paths,
        )
        PluginCommand.register_for_function(
            "RouteNinja\\Call Tree (callees) From This Function",
            "Show a forward call tree rooted at this function.",
            self.cmd_call_tree_callees,
        )
        PluginCommand.register_for_function(
            "RouteNinja\\Call Tree (callers) Of This Function",
            "Show a reverse call tree of everything that can reach this function.",
            self.cmd_call_tree_callers,
        )

    # ---- BV housekeeping -----------------------------------------------------

    def _bind_bv(self, bv: BinaryView) -> bool:
        if bv is None:
            interaction.show_message_box("RouteNinja", "No BinaryView is open.")
            return False
        if self.pt.bv is not bv:
            self.pt.set_binaryview(bv)
        return True

    def _notify_sidebar(self) -> None:
        widget = RouteNinjaSidebar.active_instance
        if widget is not None and widget.pt is self.pt:
            widget.refresh_endpoints()

    # ---- commands: set endpoints --------------------------------------------

    def cmd_set_source_function(self, bv: BinaryView, func: Function) -> None:
        if not self._bind_bv(bv):
            return
        addr = self.pt.set_source(func)
        logging.info(f"RouteNinja source = {func.name} @ {hex(addr)}")
        self._notify_sidebar()

    def cmd_set_target_function(self, bv: BinaryView, func: Function) -> None:
        if not self._bind_bv(bv):
            return
        addr = self.pt.set_target(func)
        logging.info(f"RouteNinja target = {func.name} @ {hex(addr)}")
        self._notify_sidebar()

    # ---- commands: search ----------------------------------------------------

    def _run_search(self, bv: BinaryView, show_graph: bool = True) -> List[AnnotatedPath]:
        if self.pt.source_addr is None or self.pt.target_addr is None:
            interaction.show_message_box(
                "RouteNinja", "Set both a source and a target function first."
            )
            return []
        try:
            paths = self.pt.find_paths()
        except Exception as exc:
            logging.exception("RouteNinja search failed.")
            interaction.show_message_box("RouteNinja", f"Search failed: {exc}")
            return []

        src_name = self.pt.function_name(self.pt.source_addr)
        dst_name = self.pt.function_name(self.pt.target_addr)
        if not paths:
            interaction.show_message_box(
                "RouteNinja",
                f"No path from {src_name} to {dst_name} within "
                f"{self.pt.max_depth} hops.",
            )
            self._notify_sidebar()
            return paths

        logging.info(
            f"RouteNinja: {len(paths)} path(s) from {src_name} -> {dst_name}"
        )
        self._notify_sidebar()
        if show_graph:
            show_paths(bv, paths, title=f"RouteNinja: {src_name} -> {dst_name}")
        return paths

    def cmd_find_paths(self, bv: BinaryView) -> None:
        if not self._bind_bv(bv):
            return
        self._run_search(bv, show_graph=True)

    def cmd_find_paths_to_function(self, bv: BinaryView, func: Function) -> None:
        if not self._bind_bv(bv):
            return
        if self.pt.source_addr is None:
            interaction.show_message_box(
                "RouteNinja",
                "Set a source first (RouteNinja\\Set as Source on another function).",
            )
            return
        self.pt.set_target(func)
        self._run_search(bv, show_graph=True)

    def cmd_find_paths_from_function(self, bv: BinaryView, func: Function) -> None:
        if not self._bind_bv(bv):
            return
        if self.pt.target_addr is None:
            interaction.show_message_box(
                "RouteNinja",
                "Set a target first (RouteNinja\\Set as Target on another function).",
            )
            return
        self.pt.set_source(func)
        self._run_search(bv, show_graph=True)

    def cmd_show_last_paths(self, bv: BinaryView) -> None:
        if not self._bind_bv(bv):
            return
        if not self.pt.last_paths:
            interaction.show_message_box(
                "RouteNinja", "No previous RouteNinja result to display."
            )
            return
        src_name = self.pt.function_name(self.pt.source_addr or 0)
        dst_name = self.pt.function_name(self.pt.target_addr or 0)
        show_paths(
            bv,
            self.pt.last_paths,
            title=f"RouteNinja: {src_name} -> {dst_name}",
        )

    # ---- commands: call tree ------------------------------------------------

    def _build_and_show_tree(self, bv: BinaryView, root_addr: int,
                             direction: str) -> None:
        try:
            tree = self.pt.build_call_tree(
                root=root_addr,
                direction=direction,
                max_depth=self.pt.max_depth,
            )
        except Exception as exc:
            logging.exception("RouteNinja call tree build failed.")
            interaction.show_message_box("RouteNinja", f"Tree build failed: {exc}")
            return
        show_call_tree(bv, tree)

    def cmd_call_tree_callees(self, bv: BinaryView, func: Function) -> None:
        if not self._bind_bv(bv):
            return
        self._build_and_show_tree(bv, int(func.start), "callees")

    def cmd_call_tree_callers(self, bv: BinaryView, func: Function) -> None:
        if not self._bind_bv(bv):
            return
        self._build_and_show_tree(bv, int(func.start), "callers")


# ---- Sidebar widget ---------------------------------------------------------


class RouteNinjaSidebarType(SidebarWidgetType):
    def __init__(self, pt: RouteNinja):
        icon = _load_sidebar_icon()
        self.pt = pt
        SidebarWidgetType.__init__(self, icon, "RouteNinja")

    def createWidget(self, frame, data):
        return RouteNinjaSidebar("RouteNinja", data, self.pt)

    def defaultLocation(self):
        return SidebarWidgetLocation.RightContent

    def contextSensitivity(self):
        return SidebarContextSensitivity.SelfManagedSidebarContext


class RouteNinjaSidebar(SidebarWidget):
    """Source + target pickers, search controls, result list.

    The widget listens for navigation events via notifyOffsetChanged so it
    can bind the active BinaryView to `pt` without forcing the user to
    run a plugin command first.
    """

    active_instance: Optional["RouteNinjaSidebar"] = None

    def __init__(self, name: str, data, pt: RouteNinja):
        SidebarWidget.__init__(self, name)
        self.pt = pt
        self.data = data
        self.current_offset: Optional[int] = None
        self.actionHandler = UIActionHandler()
        self.actionHandler.setupActionHandler(self)
        RouteNinjaSidebar.active_instance = self

        # ---- Endpoints group
        endpoints_group = QGroupBox("Endpoints")
        endpoints_layout = QGridLayout()

        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("function name or 0x...")
        self.source_edit.editingFinished.connect(self._on_source_edit_finished)
        self.btn_source_here = QPushButton("Use current")
        self.btn_source_here.clicked.connect(self._on_use_current_source)
        endpoints_layout.addWidget(QLabel("Source:"), 0, 0)
        endpoints_layout.addWidget(self.source_edit, 0, 1)
        endpoints_layout.addWidget(self.btn_source_here, 0, 2)

        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("function name or 0x...")
        self.target_edit.editingFinished.connect(self._on_target_edit_finished)
        self.btn_target_here = QPushButton("Use current")
        self.btn_target_here.clicked.connect(self._on_use_current_target)
        endpoints_layout.addWidget(QLabel("Target:"), 1, 0)
        endpoints_layout.addWidget(self.target_edit, 1, 1)
        endpoints_layout.addWidget(self.btn_target_here, 1, 2)

        self.btn_swap = QPushButton("Swap")
        self.btn_swap.clicked.connect(self._on_swap)
        endpoints_layout.addWidget(self.btn_swap, 2, 2)

        endpoints_layout.setColumnStretch(1, 1)
        endpoints_group.setLayout(endpoints_layout)

        # ---- Search params
        params_group = QGroupBox("Search")
        params_form = QFormLayout()

        self.spin_depth = QSpinBox()
        self.spin_depth.setRange(1, 50)
        self.spin_depth.setValue(self.pt.max_depth)
        self.spin_depth.valueChanged.connect(self._on_depth_changed)
        params_form.addRow("Max depth:", self.spin_depth)

        self.spin_paths = QSpinBox()
        self.spin_paths.setRange(1, 1000)
        self.spin_paths.setValue(self.pt.max_paths)
        self.spin_paths.valueChanged.connect(self._on_max_paths_changed)
        params_form.addRow("Max paths:", self.spin_paths)

        self.cb_merged_graph = QCheckBox("Merged graph (overlay all paths)")
        self.cb_merged_graph.setChecked(True)
        params_form.addRow(self.cb_merged_graph)

        params_group.setLayout(params_form)

        # ---- Action buttons
        btns_layout = QHBoxLayout()
        self.btn_find = QPushButton("Find paths")
        self.btn_find.clicked.connect(self._on_find_clicked)
        btns_layout.addWidget(self.btn_find)

        self.btn_show_graph = QPushButton("Show graph")
        self.btn_show_graph.clicked.connect(self._on_show_graph_clicked)
        btns_layout.addWidget(self.btn_show_graph)

        self.btn_rebuild = QPushButton("Rebuild call graph")
        self.btn_rebuild.clicked.connect(self._on_rebuild_clicked)
        btns_layout.addWidget(self.btn_rebuild)

        # ---- Call tree controls
        tree_group = QGroupBox("Call tree")
        tree_layout = QVBoxLayout()
        tree_hint = QLabel(
            "Tree root = 'Source' field above. Depth = max depth slider."
        )
        tree_hint.setStyleSheet("color: gray;")
        tree_hint.setWordWrap(True)
        tree_layout.addWidget(tree_hint)

        tree_btns = QHBoxLayout()
        self.btn_tree_callees = QPushButton("Callees tree")
        self.btn_tree_callees.setToolTip(
            "Root at top, everything the source reaches (directly or "
            "indirectly) expanded below."
        )
        self.btn_tree_callees.clicked.connect(self._on_tree_callees_clicked)
        tree_btns.addWidget(self.btn_tree_callees)

        self.btn_tree_callers = QPushButton("Callers tree")
        self.btn_tree_callers.setToolTip(
            "Root at top, every function that can reach the source expanded "
            "below."
        )
        self.btn_tree_callers.clicked.connect(self._on_tree_callers_clicked)
        tree_btns.addWidget(self.btn_tree_callers)

        tree_layout.addLayout(tree_btns)
        tree_group.setLayout(tree_layout)

        # ---- Results list
        results_group = QGroupBox("Results")
        results_layout = QVBoxLayout()
        self.status_label = QLabel("No search yet.")
        self.status_label.setWordWrap(True)
        results_layout.addWidget(self.status_label)

        self.results_list = QListWidget()
        self.results_list.itemDoubleClicked.connect(self._on_result_double_clicked)
        self.results_list.itemClicked.connect(self._on_result_clicked)
        results_layout.addWidget(self.results_list)
        results_group.setLayout(results_layout)

        self.graph_info_label = QLabel("")
        self.graph_info_label.setWordWrap(True)
        self.graph_info_label.setStyleSheet("color: gray;")

        # ---- Compose
        scroll_container = QWidget()
        scroll_inner = QVBoxLayout(scroll_container)
        scroll_inner.addWidget(endpoints_group)
        scroll_inner.addWidget(params_group)
        scroll_inner.addLayout(btns_layout)
        scroll_inner.addWidget(tree_group)
        scroll_inner.addWidget(results_group)
        scroll_inner.addWidget(self.graph_info_label)
        scroll_inner.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setWidget(scroll_container)

        layout = QVBoxLayout()
        layout.addWidget(scroll)
        self.setLayout(layout)

        self.refresh_endpoints()

    # ---- BinaryView binding via navigation events ---------------------------

    def notifyViewChanged(self, view_frame):
        if view_frame is None:
            return
        try:
            bv = view_frame.getCurrentBinaryView()
        except Exception:
            logging.exception("getCurrentBinaryView failed")
            return
        if bv is None:
            return
        if self.pt.bv is not bv:
            self.pt.set_binaryview(bv)
            self.refresh_endpoints()
            self._update_graph_info()

    def notifyOffsetChanged(self, offset: int):
        # Track the cursor offset so 'Use current' on either endpoint can
        # resolve to whichever function the user is presently reading.
        self.current_offset = int(offset)

    # ---- Event handlers: endpoints ------------------------------------------

    def _current_bv(self) -> Optional[BinaryView]:
        return self.pt.bv

    def _current_offset(self) -> Optional[int]:
        return self.current_offset

    def _current_function(self) -> Optional[Function]:
        bv = self._current_bv()
        if bv is None:
            return None
        offset = self._current_offset()
        if offset is None:
            return None
        fns = list(bv.get_functions_containing(offset))
        return fns[0] if fns else None

    def _on_source_edit_finished(self):
        bv = self._current_bv()
        if bv is None:
            return
        text = self.source_edit.text().strip()
        if not text:
            self.pt.set_source(None)
            self._update_graph_info()
            return
        fn = _resolve_address_to_function(bv, text)
        if fn is None:
            self.status_label.setText(f"No function matches '{text}'.")
            return
        self.pt.set_source(fn)
        self.refresh_endpoints()

    def _on_target_edit_finished(self):
        bv = self._current_bv()
        if bv is None:
            return
        text = self.target_edit.text().strip()
        if not text:
            self.pt.set_target(None)
            self._update_graph_info()
            return
        fn = _resolve_address_to_function(bv, text)
        if fn is None:
            self.status_label.setText(f"No function matches '{text}'.")
            return
        self.pt.set_target(fn)
        self.refresh_endpoints()

    def _on_use_current_source(self):
        fn = self._current_function()
        if fn is None:
            self.status_label.setText("Cursor is not inside a function.")
            return
        self.pt.set_source(fn)
        self.refresh_endpoints()

    def _on_use_current_target(self):
        fn = self._current_function()
        if fn is None:
            self.status_label.setText("Cursor is not inside a function.")
            return
        self.pt.set_target(fn)
        self.refresh_endpoints()

    def _on_swap(self):
        self.pt.swap_endpoints()
        self.refresh_endpoints()

    # ---- Event handlers: params + search ------------------------------------

    def _on_depth_changed(self, value: int):
        self.pt.max_depth = int(value)

    def _on_max_paths_changed(self, value: int):
        self.pt.max_paths = int(value)

    def _on_rebuild_clicked(self):
        bv = self._current_bv()
        if bv is None:
            self.status_label.setText("No BinaryView bound — click inside the main view first.")
            return
        self.pt.invalidate_graph()
        try:
            g = self.pt.ensure_graph()
            self.status_label.setText(
                f"Call graph rebuilt: {g.node_count} nodes, {g.edge_count} edges "
                f"in {g.build_time_s*1000:.0f}ms."
            )
        except Exception as exc:
            self.status_label.setText(f"Rebuild failed: {exc}")
            logging.exception("RouteNinja graph rebuild failed.")

    def _on_find_clicked(self):
        bv = self._current_bv()
        if bv is None:
            self.status_label.setText("No BinaryView bound.")
            return
        if self.pt.source_addr is None or self.pt.target_addr is None:
            self.status_label.setText("Set both source and target first.")
            return
        try:
            paths = self.pt.find_paths()
        except Exception as exc:
            self.status_label.setText(f"Search failed: {exc}")
            logging.exception("RouteNinja search failed.")
            return

        self._populate_results(paths)
        # Auto-open only when merged-graph mode is on: in that mode every
        # result collapses into a single DAG, so there's nothing to pick
        # between. With merged off, each path would need its own window —
        # wait for the user to double-click the one they want.
        if paths and self.cb_merged_graph.isChecked():
            self._show_graph_for_last(bv)

    def _on_show_graph_clicked(self):
        bv = self._current_bv()
        if bv is None:
            self.status_label.setText("No BinaryView bound.")
            return
        if not self.pt.last_paths:
            self.status_label.setText("No previous search result to display.")
            return
        self._show_graph_for_last(bv)

    def _on_tree_callees_clicked(self):
        self._run_call_tree("callees")

    def _on_tree_callers_clicked(self):
        self._run_call_tree("callers")

    def _run_call_tree(self, direction: str):
        bv = self._current_bv()
        if bv is None:
            self.status_label.setText("No BinaryView bound.")
            return
        if self.pt.source_addr is None:
            self.status_label.setText(
                "Set 'Source' first — that's the root of the tree."
            )
            return
        try:
            tree = self.pt.build_call_tree(
                root=self.pt.source_addr,
                direction=direction,
                max_depth=self.pt.max_depth,
            )
        except Exception as exc:
            self.status_label.setText(f"Tree build failed: {exc}")
            logging.exception("RouteNinja tree build failed.")
            return
        root_name = self.pt.function_name(self.pt.source_addr)
        status = (
            f"Call tree ({direction}) rooted at {root_name}: "
            f"{len(tree.nodes)} node(s), depth {self.pt.max_depth}"
            + (" [truncated]" if tree.truncated else "")
        )
        self.status_label.setText(status)
        show_call_tree(bv, tree)

    def _show_graph_for_last(self, bv: BinaryView):
        src_name = self.pt.function_name(self.pt.source_addr or 0)
        dst_name = self.pt.function_name(self.pt.target_addr or 0)
        merged = self.cb_merged_graph.isChecked()
        show_paths(
            bv,
            self.pt.last_paths,
            title=f"RouteNinja: {src_name} -> {dst_name}",
            merged=merged,
        )

    def _on_result_clicked(self, item: QListWidgetItem):
        # Nothing to do on single-click; leave space for a future preview pane.
        pass

    def _on_result_double_clicked(self, item: QListWidgetItem):
        bv = self._current_bv()
        if bv is None:
            return
        idx = self.results_list.row(item)
        if idx < 0 or idx >= len(self.pt.last_paths):
            return
        path = self.pt.last_paths[idx]
        show_paths(
            bv,
            [path],
            title=f"RouteNinja #{idx+1}: len={len(path.nodes)-1}",
            merged=True,
        )

    # ---- Refresh helpers ----------------------------------------------------

    def refresh_endpoints(self):
        if self.pt.source_addr is not None:
            self._set_line_edit_silently(
                self.source_edit,
                f"{self.pt.function_name(self.pt.source_addr)} @ {hex(self.pt.source_addr)}",
            )
        else:
            self._set_line_edit_silently(self.source_edit, "")
        if self.pt.target_addr is not None:
            self._set_line_edit_silently(
                self.target_edit,
                f"{self.pt.function_name(self.pt.target_addr)} @ {hex(self.pt.target_addr)}",
            )
        else:
            self._set_line_edit_silently(self.target_edit, "")
        self._populate_results(self.pt.last_paths, preserve_status=True)
        self._update_graph_info()

    @staticmethod
    def _set_line_edit_silently(edit: QLineEdit, text: str):
        blocker = QtCore.QSignalBlocker(edit)
        edit.setText(text)
        del blocker

    def _populate_results(self, paths: List[AnnotatedPath],
                          preserve_status: bool = False):
        self.results_list.clear()
        if not paths:
            if not preserve_status:
                self.status_label.setText("No paths found.")
            return
        for i, p in enumerate(paths):
            txt = f"#{i+1} (len {len(p.nodes)-1}): {self.pt.format_path(p)}"
            item = QListWidgetItem(txt)
            item.setToolTip(txt)
            self.results_list.addItem(item)
        if not preserve_status:
            self.status_label.setText(
                f"{len(paths)} path(s) found. Double-click any row to see just that path."
            )

    def _update_graph_info(self):
        graph = self.pt.graph
        if graph is None:
            self.graph_info_label.setText("Call graph: not yet built.")
            return
        self.graph_info_label.setText(
            f"Call graph: {graph.node_count} nodes, {graph.edge_count} edges "
            f"(built in {graph.build_time_s*1000:.0f}ms)."
        )
