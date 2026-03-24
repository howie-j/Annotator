#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')

import sys
import math
import copy
import uuid
from dataclasses import dataclass
from typing import Optional

from gi.repository import Gtk, Adw, Gdk, GdkPixbuf, Gio, Pango, PangoCairo
import cairo

# ── Constants ───────────────────────────────────────────────────────────────

VERSION    = '0.1.1'

GRID       = 10
BOX_RADIUS = 10
HANDLE_SIZE = 8
MIN_BOX_W  = 60
MIN_BOX_H  = 40
MIN_W      = 800
MIN_H      = 600
MAX_UNDO   = 50

# S / M / L size presets: arrow line width, arrowhead size, box padding x/y, font size
SIZE_PRESETS = {
    'S': dict(arrow_width=6.0, arrowhead=18, pad_x=24, pad_y=12, font_size=12),
    'M': dict(arrow_width=9.0, arrowhead=27, pad_x=36, pad_y=18, font_size=18),
    'L': dict(arrow_width=12.0, arrowhead=36, pad_x=48, pad_y=24, font_size=32),
}
DEFAULT_SIZE = 'M'

PALETTE = [
    ("#FF0000", "Red"),
    ("#FF7D00", "Orange"),
    ("#FFFF00", "Yellow"),
    ("#007D00", "Green"),
    ("#007DFF", "Blue"),
    ("#FFFFFF", "White"),
    ("#000000", "Black"),
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def snap(v):
    return round(v / GRID) * GRID

def snap_size(v, minimum):
    return max(round(v / GRID) * GRID, minimum)

def hex_to_rgb(h):
    h = h.lstrip('#')
    return int(h[0:2], 16)/255, int(h[2:4], 16)/255, int(h[4:6], 16)/255

def contrast_color(hex_color):
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return "#1E293B" if 0.299*r + 0.587*g + 0.114*b > 128 else "#F8FAFC"

def compute_box_size(text, size='M'):
    """Measure text with Pango, add size-preset padding, snap to grid."""
    p = SIZE_PRESETS[size]
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    layout = PangoCairo.create_layout(cairo.Context(surf))
    layout.set_font_description(Pango.FontDescription(f"Open Sans Bold {p['font_size']}"))
    layout.set_text(text or " ", -1)
    pw, ph = layout.get_pixel_size()
    return snap_size(pw + p['pad_x'], MIN_BOX_W), snap_size(ph + p['pad_y'], MIN_BOX_H)

def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x+r,   y+r,   r, math.pi,     3*math.pi/2)
    cr.arc(x+w-r, y+r,   r, 3*math.pi/2, 2*math.pi)
    cr.arc(x+w-r, y+h-r, r, 0,            math.pi/2)
    cr.arc(x+r,   y+h-r, r, math.pi/2,   math.pi)
    cr.close_path()

def draw_arrowhead(cr, x, y, angle, size):
    cr.save()
    cr.translate(x, y)
    cr.rotate(angle)
    cr.move_to(0, 0)
    cr.line_to(-size, -size * 0.4)
    cr.line_to(-size * 0.7, 0)
    cr.line_to(-size,  size * 0.4)
    cr.close_path()
    cr.fill()
    cr.restore()

# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Annotation:
    id: str
    text: str
    box_x: int
    box_y: int
    box_w: int
    box_h: int
    head_x: float
    head_y: float
    color: str
    size: str = 'M'
    ann_type: str = 'textbox'   # 'textbox' | 'text' | 'arrow'

    def copy(self):
        return copy.copy(self)

# ── Canvas ───────────────────────────────────────────────────────────────────

class AnnotatorCanvas(Gtk.DrawingArea):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.image: Optional[GdkPixbuf.Pixbuf] = None
        self.annotations: list[Annotation] = []
        self.undo_stack = []
        self.redo_stack = []

        self.drag_mode = None    # 'box' | 'head'
        self.drag_ann_id = None
        self.drag_start = (0, 0) # canvas-space press point
        self.drag_orig = None    # copy of annotation at drag start
        self._drag_orig_all: dict = {}  # id → (box_x, box_y) for multi-box drag

        self.selected_ids: list[str] = []  # ordered; [0] is primary for toolbar sync
        self._ann_clipboard: list[Annotation] = []
        self._ctx_pos = (0.0, 0.0)         # canvas coords of last right-click

        self.tool_mode: str = 'textbox'           # 'select' | 'textbox' | 'text' | 'arrow'
        self.rubber_band_start: tuple | None = None   # canvas coords
        self.rubber_band_end:   tuple | None = None   # canvas coords
        self.creating_arrow_tail: tuple | None = None # image coords, grid-snapped
        self.creating_arrow_head: tuple | None = None # image coords, free

        self.editing_id: str | None = None        # annotation being edited inline
        self.editing_is_new: bool = False
        self._edit_prev_snapshot: list | None = None  # snapshot before edit started
        self._edit_anchor: tuple | None = None    # image-space center of box during edit

        self._ox = 0             # image offset within canvas (for centering)
        self._oy = 0

        self.set_focusable(True)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_size_request(MIN_W, MIN_H)
        self.set_draw_func(self._draw)

        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_press)
        self.add_controller(click)

        rclick = Gtk.GestureClick.new()
        rclick.set_button(3)
        rclick.connect("pressed", self._on_right_click)
        self.add_controller(rclick)

        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end",    self._on_drag_end)
        self.add_controller(drag)

        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self._ctx_popover = None
        self.connect("realize", lambda *_: self.grab_focus())

    def set_tool_mode(self, mode: str):
        if self.editing_id:
            if self.editing_is_new:
                self._cancel_edit()
            else:
                self._commit_edit()
        self.tool_mode = mode
        self.win.sync_tool_button(mode)
        self.queue_draw()

    # ── Context menu ─────────────────────────────────────────────────────────

    def _build_context_menu(self):
        model = Gio.Menu()
        tools = [
            ('textbox', 'Callout', 'win.tool-textbox'),
            ('text',    'Label',   'win.tool-text'),
            ('arrow',   'Arrow',    'win.tool-arrow'),
        ]
        if self.tool_mode != 'select':
            # "Leave Tool" alone in its own section, then remaining tools in next section
            s_leave = Gio.Menu()
            s_leave.append("Leave Tool", "win.leave-tool")
            model.append_section(None, s_leave)
            s_other = Gio.Menu()
            for mode, label, action in tools:
                if mode != self.tool_mode:
                    s_other.append(label, action)
            model.append_section(None, s_other)
        else:
            # All three tools in one section
            s_tools = Gio.Menu()
            for mode, label, action in tools:
                s_tools.append(label, action)
            model.append_section(None, s_tools)
        s1 = Gio.Menu()
        s1.append("Cut",    "win.cut")
        s1.append("Copy",   "win.copy")
        s1.append("Paste",  "win.paste")
        s1.append("Delete", "win.delete")
        s2 = Gio.Menu()
        s2.append("Undo",   "win.undo")
        s2.append("Redo",   "win.redo")
        s3 = Gio.Menu()
        s3.append("Select All", "win.select-all")
        model.append_section(None, s1)
        model.append_section(None, s2)
        model.append_section(None, s3)
        popover = Gtk.PopoverMenu.new_from_model(model)
        popover.set_parent(self)
        popover.set_has_arrow(False)
        return popover

    def _on_right_click(self, gesture, n_press, x, y):
        self.grab_focus()
        self._ctx_pos = (x, y)
        if self._ctx_popover:
            self._ctx_popover.unparent()
        self._ctx_popover = self._build_context_menu()
        rect = Gdk.Rectangle()
        rect.x = int(x); rect.y = int(y)
        rect.width = 1;  rect.height = 1
        self._ctx_popover.set_pointing_to(rect)
        self._ctx_popover.popup()

    # ── Clipboard ────────────────────────────────────────────────────────────

    def cut_selected(self):
        if not self.selected_ids:
            return
        self.copy_selected()
        self._push_undo(self._snapshot())
        ids = set(self.selected_ids)
        self.annotations = [a for a in self.annotations if a.id not in ids]
        self.selected_ids = []
        self.queue_draw()

    def copy_selected(self):
        ids = set(self.selected_ids)
        self._ann_clipboard = [a.copy() for a in self.annotations if a.id in ids]

    def paste_annotations(self):
        if not self._ann_clipboard:
            return
        self._push_undo(self._snapshot())
        new_anns = []
        for a in self._ann_clipboard:
            n = a.copy()
            n.id    = str(uuid.uuid4())
            n.box_x = snap(a.box_x + GRID)
            n.box_y = snap(a.box_y + GRID)
            n.head_x = a.head_x + GRID
            n.head_y = a.head_y + GRID
            new_anns.append(n)
        self.annotations.extend(new_anns)
        self._select([a.id for a in new_anns])
        self.queue_draw()

    # ── Undo / Redo ──────────────────────────────────────────────────────────

    def _snapshot(self):
        return [a.copy() for a in self.annotations]

    def _push_undo(self, snapshot):
        self.undo_stack.append(snapshot)
        if len(self.undo_stack) > MAX_UNDO:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(self._snapshot())
        self.annotations = self.undo_stack.pop()
        valid = {a.id for a in self.annotations}
        self.selected_ids = [i for i in self.selected_ids if i in valid]
        self.queue_draw()

    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(self._snapshot())
        self.annotations = self.redo_stack.pop()
        self.queue_draw()

    # ── Image loading ────────────────────────────────────────────────────────

    def load_image(self, pixbuf: GdkPixbuf.Pixbuf):
        self.image = pixbuf
        self.annotations.clear()
        self.selected_ids = []
        self.undo_stack.clear()
        self.redo_stack.clear()
        iw, ih = pixbuf.get_width(), pixbuf.get_height()
        self.set_size_request(max(MIN_W, iw), max(MIN_H, ih))
        self.queue_draw()

    def _select(self, ids: list[str]):
        """Set selection and sync toolbar to primary (first) annotation."""
        self.selected_ids = ids
        if ids:
            ann = self._get_ann(ids[0])
            if ann:
                self.win.sync_toolbar(ann)

    # ── Inline text editing ──────────────────────────────────────────────────

    def _start_edit(self, ann_id: str, is_new: bool = False):
        if self.editing_id and self.editing_id != ann_id:
            self._commit_edit()
        self._edit_prev_snapshot = self._snapshot()
        self.editing_id   = ann_id
        self.editing_is_new = is_new
        ann = self._get_ann(ann_id)
        if ann:
            self._edit_anchor = (ann.box_x + ann.box_w / 2, ann.box_y + ann.box_h / 2)
        self._select([ann_id])
        self.queue_draw()

    def _commit_edit(self):
        if not self.editing_id:
            return
        ann = self._get_ann(self.editing_id)
        if ann and not ann.text.strip():
            # Empty → remove silently, no undo entry
            self.annotations = [a for a in self.annotations if a.id != ann.id]
            self.selected_ids = []
        elif ann and self._edit_prev_snapshot is not None:
            self._push_undo(self._edit_prev_snapshot)
        self.editing_id         = None
        self.editing_is_new     = False
        self._edit_prev_snapshot = None
        self._edit_anchor       = None
        self.queue_draw()

    def _cancel_edit(self):
        if not self.editing_id:
            return
        if self._edit_prev_snapshot is not None:
            self.annotations = self._edit_prev_snapshot
            valid = {a.id for a in self.annotations}
            self.selected_ids = [i for i in self.selected_ids if i in valid]
        self.editing_id         = None
        self.editing_is_new     = False
        self._edit_prev_snapshot = None
        self._edit_anchor       = None
        self.queue_draw()

    def _resize_box_centered(self, ann: 'Annotation'):
        """Resize annotation box keeping center at self._edit_anchor."""
        if not self._edit_anchor or not self.image:
            ann.box_w, ann.box_h = compute_box_size(ann.text or ' ', ann.size)
            return
        iw, ih = self.image.get_width(), self.image.get_height()
        ann.box_w, ann.box_h = compute_box_size(ann.text or ' ', ann.size)
        ax, ay = self._edit_anchor
        ann.box_x = max(0, min(snap(ax - ann.box_w // 2), iw - ann.box_w))
        ann.box_y = max(0, min(snap(ay - ann.box_h // 2), ih - ann.box_h))

    # ── Coordinate helpers ───────────────────────────────────────────────────

    def _to_img(self, cx, cy):
        """Canvas → image coordinates."""
        return cx - self._ox, cy - self._oy

    # ── Hit testing ──────────────────────────────────────────────────────────

    def _hit_arrowhead(self, cx, cy):
        ix, iy = self._to_img(cx, cy)
        for ann in reversed(self.annotations):
            if ann.ann_type in ('textbox', 'arrow'):
                if math.hypot(ix - ann.head_x, iy - ann.head_y) <= HANDLE_SIZE + 2:
                    return ann.id
        return None

    def _hit_arrow_tail(self, cx, cy):
        ix, iy = self._to_img(cx, cy)
        for ann in reversed(self.annotations):
            if ann.ann_type == 'arrow':
                if math.hypot(ix - ann.box_x, iy - ann.box_y) <= HANDLE_SIZE + 2:
                    return ann.id
        return None

    def _hit_box(self, cx, cy):
        ix, iy = self._to_img(cx, cy)
        for ann in reversed(self.annotations):
            if ann.ann_type == 'arrow':
                # Distance from point to line segment
                dx = ann.head_x - ann.box_x
                dy = ann.head_y - ann.box_y
                len2 = dx*dx + dy*dy
                if len2 == 0:
                    dist = math.hypot(ix - ann.box_x, iy - ann.box_y)
                else:
                    t = max(0.0, min(1.0, ((ix - ann.box_x)*dx + (iy - ann.box_y)*dy) / len2))
                    dist = math.hypot(ix - (ann.box_x + t*dx), iy - (ann.box_y + t*dy))
                if dist <= 8:
                    return ann.id
            else:
                if ann.box_x <= ix <= ann.box_x + ann.box_w and \
                   ann.box_y <= iy <= ann.box_y + ann.box_h:
                    return ann.id
        return None

    def _get_ann(self, ann_id) -> Optional[Annotation]:
        return next((a for a in self.annotations if a.id == ann_id), None)

    # ── Drawing ──────────────────────────────────────────────────────────────

    def _draw(self, area, cr, width, height):
        cr.set_source_rgb(0.12, 0.12, 0.13)
        cr.paint()

        if not self.image:
            cr.set_source_rgba(1, 1, 1, 0.3)
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription("Open Sans Bold 16"))
            layout.set_text("Open, paste, or drop an image to begin", -1)
            pw, ph = layout.get_pixel_size()
            cr.move_to((width - pw) / 2, (height - ph) / 2)
            PangoCairo.show_layout(cr, layout)
            return

        iw, ih = self.image.get_width(), self.image.get_height()
        self._ox = max(0, (width  - iw) // 2)
        self._oy = max(0, (height - ih) // 2)

        cr.save()
        cr.translate(self._ox, self._oy)

        Gdk.cairo_set_source_pixbuf(cr, self.image, 0, 0)
        cr.paint()

        for ann in self.annotations:
            self._draw_annotation(cr, ann)

        for sid in self.selected_ids:
            ann = self._get_ann(sid)
            if ann:
                self._draw_selection(cr, ann)

        # Arrow creation preview
        if self.creating_arrow_tail and self.creating_arrow_head:
            tx, ty = self.creating_arrow_tail
            hx, hy = self.creating_arrow_head
            if math.hypot(hx - tx, hy - ty) > 2:
                p = SIZE_PRESETS[self.win.current_size]
                r, g, b = hex_to_rgb(self.win.current_color)
                angle = math.atan2(hy - ty, hx - tx)
                tip_x = hx - p['arrowhead'] * 0.7 * math.cos(angle)
                tip_y = hy - p['arrowhead'] * 0.7 * math.sin(angle)
                cr.set_source_rgba(r, g, b, 0.7)
                cr.set_line_width(p['arrow_width'])
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.move_to(tx, ty)
                cr.line_to(tip_x, tip_y)
                cr.stroke()
                cr.set_source_rgba(r, g, b, 0.7)
                draw_arrowhead(cr, hx, hy, angle, p['arrowhead'])

        cr.restore()

        if self.rubber_band_start and self.rubber_band_end:
            x1, y1 = self.rubber_band_start
            x2, y2 = self.rubber_band_end
            rx, ry = min(x1, x2), min(y1, y2)
            rw, rh = abs(x2 - x1), abs(y2 - y1)
            cr.set_source_rgba(0.2, 0.5, 1.0, 0.25)
            cr.rectangle(rx, ry, rw, rh)
            cr.fill()
            cr.set_source_rgba(0.2, 0.5, 1.0, 0.9)
            cr.set_line_width(1.5)
            cr.set_dash([4.0, 4.0])
            cr.rectangle(rx, ry, rw, rh)
            cr.stroke()
            cr.set_dash([])

    def _draw_annotation(self, cr, ann: Annotation):
        if ann.ann_type == 'textbox':
            self._draw_textbox(cr, ann)
        elif ann.ann_type == 'text':
            self._draw_text(cr, ann)
        elif ann.ann_type == 'arrow':
            self._draw_arrow(cr, ann)

    def _draw_textbox(self, cr, ann: Annotation):
        p  = SIZE_PRESETS[ann.size]
        r, g, b    = hex_to_rgb(ann.color)
        tr, tg, tb = hex_to_rgb(contrast_color(ann.color))
        # Arrow from box center — box covers the tail
        cx    = ann.box_x + ann.box_w / 2
        cy    = ann.box_y + ann.box_h / 2
        angle = math.atan2(ann.head_y - cy, ann.head_x - cx)
        tip_x = ann.head_x - p['arrowhead'] * 0.7 * math.cos(angle)
        tip_y = ann.head_y - p['arrowhead'] * 0.7 * math.sin(angle)
        cr.set_source_rgb(r, g, b)
        cr.set_line_width(p['arrow_width'])
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        cr.move_to(cx, cy)
        cr.line_to(tip_x, tip_y)
        cr.stroke()
        draw_arrowhead(cr, ann.head_x, ann.head_y, angle, p['arrowhead'])
        rounded_rect(cr, ann.box_x, ann.box_y, ann.box_w, ann.box_h, BOX_RADIUS)
        cr.set_source_rgb(r, g, b)
        cr.fill()
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"Open Sans Bold {SIZE_PRESETS[ann.size]['font_size']}"))
        layout.set_text(ann.text, -1)
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_width(Pango.units_from_double(ann.box_w))
        _, ph = layout.get_pixel_size()
        text_y = ann.box_y + (ann.box_h - ph) / 2
        cr.move_to(ann.box_x, text_y)
        cr.set_source_rgb(tr, tg, tb)
        PangoCairo.show_layout(cr, layout)
        if self.editing_id == ann.id:
            self._draw_cursor(cr, ann, layout, text_y)

    def _draw_text(self, cr, ann: Annotation):
        r, g, b    = hex_to_rgb(ann.color)
        tr, tg, tb = hex_to_rgb(contrast_color(ann.color))
        rounded_rect(cr, ann.box_x, ann.box_y, ann.box_w, ann.box_h, BOX_RADIUS)
        cr.set_source_rgb(r, g, b)
        cr.fill()
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"Open Sans Bold {SIZE_PRESETS[ann.size]['font_size']}"))
        layout.set_text(ann.text, -1)
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_width(Pango.units_from_double(ann.box_w))
        _, ph = layout.get_pixel_size()
        text_y = ann.box_y + (ann.box_h - ph) / 2
        cr.move_to(ann.box_x, text_y)
        cr.set_source_rgb(tr, tg, tb)
        PangoCairo.show_layout(cr, layout)
        if self.editing_id == ann.id:
            self._draw_cursor(cr, ann, layout, text_y)

    def _draw_arrow(self, cr, ann: Annotation):
        p = SIZE_PRESETS[ann.size]
        r, g, b = hex_to_rgb(ann.color)
        tx, ty = ann.box_x, ann.box_y
        hx, hy = ann.head_x, ann.head_y
        angle = math.atan2(hy - ty, hx - tx)
        tip_x = hx - p['arrowhead'] * 0.7 * math.cos(angle)
        tip_y = hy - p['arrowhead'] * 0.7 * math.sin(angle)
        cr.set_source_rgb(r, g, b)
        cr.set_line_width(p['arrow_width'])
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(tx, ty)
        cr.line_to(tip_x, tip_y)
        cr.stroke()
        draw_arrowhead(cr, hx, hy, angle, p['arrowhead'])

    def _draw_handle(self, cr, x, y, color):
        """Draw a drag handle: white halo + filled color circle."""
        r, g, b = hex_to_rgb(color)
        cr.set_source_rgb(1, 1, 1)
        cr.arc(x, y, HANDLE_SIZE/2 + 2, 0, 2*math.pi)
        cr.fill()
        cr.set_source_rgb(r, g, b)
        cr.arc(x, y, HANDLE_SIZE/2, 0, 2*math.pi)
        cr.fill()

    def _draw_box_outline(self, cr, ann: Annotation):
        """Draw white selection outline around a text/textbox box."""
        cr.set_source_rgba(1, 1, 1, 0.75)
        rounded_rect(cr, ann.box_x - 2, ann.box_y - 2, ann.box_w + 4, ann.box_h + 4, BOX_RADIUS + 2)
        cr.set_line_width(2)
        cr.stroke()

    def _draw_cursor(self, cr, ann: Annotation, layout, text_y: float):
        """Draw a non-blinking cursor at the end of text."""
        byte_idx = len(ann.text.encode('utf-8'))
        strong, _ = layout.get_cursor_pos(byte_idx)
        cx = ann.box_x + strong.x / Pango.SCALE
        cy = text_y    + strong.y / Pango.SCALE
        ch = strong.height / Pango.SCALE
        if ch < 1:
            ch = SIZE_PRESETS[ann.size]['font_size']
        tr, tg, tb = hex_to_rgb(contrast_color(ann.color))
        cr.set_source_rgb(tr, tg, tb)
        cr.set_line_width(1.5)
        cr.move_to(cx, cy)
        cr.line_to(cx, cy + ch)
        cr.stroke()

    def _draw_selection(self, cr, ann: Annotation):
        if ann.ann_type == 'arrow':
            self._draw_handle(cr, ann.box_x, ann.box_y, ann.color)
            self._draw_handle(cr, ann.head_x, ann.head_y, ann.color)
        elif ann.ann_type == 'text':
            self._draw_box_outline(cr, ann)
        else:  # textbox
            self._draw_box_outline(cr, ann)
            self._draw_handle(cr, ann.head_x, ann.head_y, ann.color)

    # ── Input ────────────────────────────────────────────────────────────────

    def _on_press(self, gesture, n_press, cx, cy):
        self.grab_focus()
        if not self.image:
            return

        # Commit any active inline edit on any click
        if self.editing_id:
            self._commit_edit()

        aid = self._hit_arrowhead(cx, cy)
        if aid:
            self._select([aid])
            self.drag_mode   = 'head'
            self.drag_ann_id = aid
            self.drag_start  = (cx, cy)
            self.drag_orig   = self._get_ann(aid).copy()
            self._push_undo(self._snapshot())
            self.queue_draw()
            return

        aid = self._hit_arrow_tail(cx, cy)
        if aid:
            self._select([aid])
            self.drag_mode   = 'arrow_tail'
            self.drag_ann_id = aid
            self.drag_start  = (cx, cy)
            self.drag_orig   = self._get_ann(aid).copy()
            self._push_undo(self._snapshot())
            self.queue_draw()
            return

        aid = self._hit_box(cx, cy)
        if aid:
            ann = self._get_ann(aid)
            if aid in self.selected_ids and n_press == 2 and len(self.selected_ids) == 1:
                if ann and ann.ann_type != 'arrow':
                    self._start_edit(aid)
                    return
            if aid not in self.selected_ids:
                self._select([aid])
                self._drag_orig_all = {}   # box-only drag for newly selected
            else:
                self._drag_orig_all = {    # whole-object drag for already selected
                    a.id: (a.box_x, a.box_y, a.head_x, a.head_y)
                    for a in self.annotations if a.id in self.selected_ids
                }
            self.drag_mode   = 'box'
            self.drag_ann_id = aid
            self.drag_start  = (cx, cy)
            self.drag_orig   = self._get_ann(aid).copy()
            self._push_undo(self._snapshot())
            self.queue_draw()
            return

        self.selected_ids = []
        self.drag_mode    = None
        self.queue_draw()
        if self.tool_mode == 'textbox':
            ix, iy = self._to_img(cx, cy)
            self._create_annotation_inline(ix, iy, 'textbox')
        elif self.tool_mode == 'text':
            ix, iy = self._to_img(cx, cy)
            self._create_annotation_inline(ix, iy, 'text')
        elif self.tool_mode == 'arrow':
            ix, iy = self._to_img(cx, cy)
            self.drag_mode = 'creating_arrow'
            self.creating_arrow_tail = (snap(ix), snap(iy))
            self.creating_arrow_head = (float(ix), float(iy))
            self.drag_start = (cx, cy)
        elif self.tool_mode == 'select':
            self.drag_mode = 'rubber_band'
            self.rubber_band_start = (cx, cy)
            self.rubber_band_end   = (cx, cy)
            self.drag_start        = (cx, cy)

    def _on_drag_update(self, gesture, ox, oy):
        if self.drag_mode == 'rubber_band':
            ok, sx, sy = gesture.get_start_point()
            if ok:
                self.rubber_band_end = (sx + ox, sy + oy)
            self.queue_draw()
            return

        if self.drag_mode == 'creating_arrow':
            ok, sx, sy = gesture.get_start_point()
            if ok:
                ix, iy = self._to_img(sx + ox, sy + oy)
                self.creating_arrow_head = (ix, iy)
            self.queue_draw()
            return

        ok, sx, sy = gesture.get_start_point()
        if not ok or not self.drag_ann_id:
            return
        dx = sx + ox - self.drag_start[0]
        dy = sy + oy - self.drag_start[1]
        ann  = self._get_ann(self.drag_ann_id)
        orig = self.drag_orig
        if not ann:
            return
        iw = self.image.get_width()  if self.image else 10000
        ih = self.image.get_height() if self.image else 10000

        if self.drag_mode == 'box':
            if self._drag_orig_all:
                # Already-selected drag: move whole object (box + head)
                for a in self.annotations:
                    if a.id not in self._drag_orig_all:
                        continue
                    bx, by, hx, hy = self._drag_orig_all[a.id]
                    if a.ann_type == 'arrow':
                        new_bx = max(0, min(snap(bx + dx), iw))
                        new_by = max(0, min(snap(by + dy), ih))
                        a.box_x  = new_bx;  a.box_y  = new_by
                        a.head_x = max(0, min(hx + (new_bx - bx), iw))
                        a.head_y = max(0, min(hy + (new_by - by), ih))
                    else:
                        a.box_x  = max(0, min(snap(bx + dx), iw - a.box_w))
                        a.box_y  = max(0, min(snap(by + dy), ih - a.box_h))
                        a.head_x = max(0, min(hx + dx, iw))
                        a.head_y = max(0, min(hy + dy, ih))
            else:
                # Newly-selected drag: move box only (head stays, points at target)
                ann.box_x = max(0, min(snap(orig.box_x + dx), iw - ann.box_w))
                ann.box_y = max(0, min(snap(orig.box_y + dy), ih - ann.box_h))
        elif self.drag_mode == 'arrow_tail':
            ann.box_x = max(0, min(snap(orig.box_x + dx), iw))
            ann.box_y = max(0, min(snap(orig.box_y + dy), ih))
        elif self.drag_mode == 'head':
            ann.head_x = max(0, min(orig.head_x + dx, iw))
            ann.head_y = max(0, min(orig.head_y + dy, ih))
        self.queue_draw()

    def _on_drag_end(self, gesture, ox, oy):
        if self.drag_mode == 'rubber_band' and self.rubber_band_start and self.rubber_band_end:
            ix1, iy1 = self._to_img(*self.rubber_band_start)
            ix2, iy2 = self._to_img(*self.rubber_band_end)
            rx1, rx2 = min(ix1, ix2), max(ix1, ix2)
            ry1, ry2 = min(iy1, iy2), max(iy1, iy2)
            hit_ids = []
            for a in self.annotations:
                if a.ann_type == 'arrow':
                    ax1 = min(a.box_x, a.head_x); ax2 = max(a.box_x, a.head_x)
                    ay1 = min(a.box_y, a.head_y); ay2 = max(a.box_y, a.head_y)
                    if ax1 < rx2 and ax2 > rx1 and ay1 < ry2 and ay2 > ry1:
                        hit_ids.append(a.id)
                else:
                    if a.box_x < rx2 and a.box_x + a.box_w > rx1 \
                            and a.box_y < ry2 and a.box_y + a.box_h > ry1:
                        hit_ids.append(a.id)
            if hit_ids:
                self._select(hit_ids)

        if self.drag_mode == 'creating_arrow' and self.creating_arrow_tail:
            tail = self.creating_arrow_tail
            head = self.creating_arrow_head or tail
            if math.hypot(head[0] - tail[0], head[1] - tail[1]) > 5:
                ann = Annotation(
                    id        = str(uuid.uuid4()),
                    ann_type  = 'arrow',
                    text      = '',
                    box_x     = int(tail[0]),
                    box_y     = int(tail[1]),
                    box_w     = 0,
                    box_h     = 0,
                    head_x    = float(head[0]),
                    head_y    = float(head[1]),
                    color     = self.win.current_color,
                    size      = self.win.current_size,
                )
                self._push_undo(self._snapshot())
                self.annotations.append(ann)
                self._select([ann.id])

        self.rubber_band_start   = None
        self.rubber_band_end     = None
        self.creating_arrow_tail = None
        self.creating_arrow_head = None
        self.drag_mode      = None
        self.drag_ann_id    = None
        self._drag_orig_all = {}
        self.queue_draw()

    def _on_key_pressed(self, ctrl, keyval, keycode, state):
        ctrl_ = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)

        # ── Inline edit mode ─────────────────────────────────────────────────
        if self.editing_id:
            ann = self._get_ann(self.editing_id)
            if not ann:
                self.editing_id = None
                return False

            if keyval == Gdk.KEY_Escape:
                self._cancel_edit()
                return True

            if keyval == Gdk.KEY_Return:
                if shift:
                    ann.text += '\n'
                    self._resize_box_centered(ann)
                    self.queue_draw()
                else:
                    self._commit_edit()
                    self.selected_ids = []
                    self.queue_draw()
                return True

            if keyval == Gdk.KEY_BackSpace and not ctrl_:
                if ann.text:
                    ann.text = ann.text[:-1]
                self._resize_box_centered(ann)
                self.queue_draw()
                return True

            # Printable character
            if not ctrl_:
                code = Gdk.keyval_to_unicode(keyval)
                if code:
                    char = chr(code)
                    if char.isprintable():
                        ann.text += char
                        self._resize_box_centered(ann)
                        self.queue_draw()
                        return True

            return True  # consume all other keys while editing

        # ── Normal mode ──────────────────────────────────────────────────────
        if ctrl_ and keyval == Gdk.KEY_z:  self.undo(); return True
        if ctrl_ and (keyval == Gdk.KEY_y or (shift and keyval == Gdk.KEY_z)):
                                           self.redo(); return True
        if ctrl_ and keyval == Gdk.KEY_o:  self.win.open_file();  return True
        if ctrl_ and keyval == Gdk.KEY_n:  self.win.new_window(); return True
        if ctrl_ and keyval == Gdk.KEY_s:
            if shift: self.win.save_as()
            else:     self.win.save()
            return True
        if ctrl_ and keyval == Gdk.KEY_x:
            self.cut_selected();   return True
        if ctrl_ and keyval == Gdk.KEY_c:
            self.copy_selected();  return True
        if ctrl_ and keyval == Gdk.KEY_v:
            if self._ann_clipboard:
                self.paste_annotations()
            else:
                self.win.paste_image()
            return True

        if ctrl_ and keyval == Gdk.KEY_a:
            self._select([a.id for a in self.annotations])
            self.queue_draw()
            return True

        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and self.selected_ids:
            self._push_undo(self._snapshot())
            ids = set(self.selected_ids)
            self.annotations = [a for a in self.annotations if a.id not in ids]
            self.selected_ids = []
            self.queue_draw()
            return True

        if keyval == Gdk.KEY_Escape:
            if self.tool_mode != 'select':
                self.set_tool_mode('select')
            self.selected_ids = []
            self.queue_draw()
            return True

        # ── Start typing creates annotation at image center ──────────────────
        if self.tool_mode in ('textbox', 'text') and self.image and not ctrl_:
            code = Gdk.keyval_to_unicode(keyval)
            char = chr(code) if code else None
            if char and char.isprintable():
                cx = self.image.get_width()  / 2
                cy = self.image.get_height() / 2
                self._create_annotation_inline(cx, cy, self.tool_mode)
                ann = self._get_ann(self.editing_id)
                if ann:
                    ann.text = char
                    self._resize_box_centered(ann)
                    self.queue_draw()
                return True

        return False

    def _on_motion(self, ctrl, x, y):
        if   self.image and self._hit_arrowhead(x, y):  self.set_cursor(Gdk.Cursor.new_from_name("crosshair", None))
        elif self.image and self._hit_arrow_tail(x, y): self.set_cursor(Gdk.Cursor.new_from_name("crosshair", None))
        elif self.image and self._hit_box(x, y):         self.set_cursor(Gdk.Cursor.new_from_name("move",      None))
        else:                                             self.set_cursor(None)

    def _on_drop(self, drop_target, value, x, y):
        if isinstance(value, Gdk.FileList):
            files = value.get_files()
            if files and (path := files[0].get_path()):
                try:
                    self.load_image(GdkPixbuf.Pixbuf.new_from_file(path))
                except Exception as e:
                    print(f"Drop error: {e}")
            return True
        return False

    # ── Annotation CRUD ──────────────────────────────────────────────────────

    def _create_annotation_inline(self, ix, iy, ann_type='textbox'):
        iw = self.image.get_width()  if self.image else 10000
        ih = self.image.get_height() if self.image else 10000
        bw, bh = compute_box_size(' ', self.win.current_size)
        bx = max(0, min(snap(ix - bw // 2), iw - bw))
        by = max(0, min(snap(iy - bh // 2), ih - bh))
        hx = max(0, min(ix + 80, iw)) if ann_type == 'textbox' else 0.0
        hy = max(0, min(iy - 40, ih)) if ann_type == 'textbox' else 0.0
        ann = Annotation(
            id       = str(uuid.uuid4()),
            ann_type = ann_type,
            text     = '',
            box_x    = bx,  box_y  = by,
            box_w    = bw,  box_h  = bh,
            head_x   = hx,  head_y = hy,
            color    = self.win.current_color,
            size     = self.win.current_size,
        )
        prev = self._snapshot()          # snapshot before the new annotation exists
        self.annotations.append(ann)
        self._start_edit(ann.id, is_new=True)
        self._edit_prev_snapshot = prev  # override so cancel fully removes it

    # ── Export ───────────────────────────────────────────────────────────────

    def render_to_surface(self):
        if not self.image:
            return None
        iw, ih = self.image.get_width(), self.image.get_height()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, iw, ih)
        cr   = cairo.Context(surf)
        Gdk.cairo_set_source_pixbuf(cr, self.image, 0, 0)
        cr.paint()
        for ann in self.annotations:
            self._draw_annotation(cr, ann)
        return surf

# ── Window ────────────────────────────────────────────────────────────────────

class AnnotatorWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_default_size(MIN_W, MIN_H)

        self.current_color = PALETTE[0][0]
        self.current_size  = DEFAULT_SIZE
        self._syncing          = False   # guard for all toolbar sync operations
        self._save_path: Optional[str] = None

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self._build_header())
        self.canvas = AnnotatorCanvas(self)
        toolbar_view.set_content(self.canvas)
        self.set_content(toolbar_view)

        self._register_actions()
        self.sync_tool_button(self.canvas.tool_mode)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _register_actions(self):
        def add(name, cb):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p: cb())
            self.add_action(action)

        add("new-window",  self.new_window)
        add("save",        self.save)
        add("save-as",     self.save_as)
        add("discard",     self.discard_changes)
        add("leave-tool",    lambda: self.canvas.set_tool_mode('select'))
        add("tool-textbox",  lambda: self.canvas.set_tool_mode('textbox'))
        add("tool-text",     lambda: self.canvas.set_tool_mode('text'))
        add("tool-arrow",    lambda: self.canvas.set_tool_mode('arrow'))
        add("cut",         self.canvas.cut_selected)
        add("copy",        self.canvas.copy_selected)
        add("paste",       self._action_paste)
        add("delete",      self._action_delete)
        add("undo",        self.canvas.undo)
        add("redo",        self.canvas.redo)
        add("select-all",  self._action_select_all)
        add("about",       self.show_about)

    def _action_paste(self):
        if self.canvas._ann_clipboard:
            self.canvas.paste_annotations()
        else:
            self.paste_image()

    def _action_delete(self):
        if self.canvas.selected_ids:
            self.canvas._push_undo(self.canvas._snapshot())
            ids = set(self.canvas.selected_ids)
            self.canvas.annotations = [a for a in self.canvas.annotations if a.id not in ids]
            self.canvas.selected_ids = []
            self.canvas.queue_draw()

    def _action_select_all(self):
        self.canvas._select([a.id for a in self.canvas.annotations])
        self.canvas.queue_draw()

    # ── Header ───────────────────────────────────────────────────────────────

    def _build_header(self):
        header = Adw.HeaderBar()
        header.set_centering_policy(Adw.CenteringPolicy.STRICT)
        header.set_title_widget(Gtk.Box())  # empty title

        # ── LEFT: open button + tool buttons ─────────────────────────────────
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        open_btn = Gtk.Button(icon_name="document-open-symbolic",
                              tooltip_text="Open image (Ctrl+O)")
        open_btn.connect("clicked", lambda *_: self.open_file())
        left.append(open_btn)

        sep_tools = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep_tools.set_margin_start(4); sep_tools.set_margin_end(4)
        left.append(sep_tools)

        tool_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tool_box.add_css_class("linked")
        self._tool_buttons = {}
        for mode, label, tooltip in [
            ('textbox', 'Callout', 'Create a labeled callout with an arrow'),
            ('text',    'Label',   'Create a text label without an arrow'),
            ('arrow',   'Arrow',   'Draw a freestanding arrow'),
        ]:
            btn = Gtk.ToggleButton(label=label, tooltip_text=tooltip)
            btn.set_active(False)
            self._tool_buttons[mode] = btn
            tool_box.append(btn)
        for mode in self._tool_buttons:
            self._tool_buttons[mode].connect("toggled", self._make_tool_handler(mode))
        left.append(tool_box)
        header.pack_start(left)

        # ── RIGHT: color palette, size, font, menu ───────────────────────────
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        # Color palette
        palette_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        palette_box.add_css_class("linked")
        self._color_buttons = []
        for i, (color, name) in enumerate(PALETTE):
            btn = Gtk.ToggleButton(tooltip_text=name)
            btn.set_size_request(24, 24)
            dot = Gtk.DrawingArea()
            dot.set_size_request(16, 16)
            r, g, b = hex_to_rgb(color)
            def make_dot(dr, dg, db):
                def draw(area, cr, w, h):
                    cr.set_source_rgb(dr, dg, db)
                    cr.arc(w/2, h/2, min(w,h)/2 - 1, 0, 2*math.pi)
                    cr.fill()
                    cr.set_source_rgba(0, 0, 0, 0.3)
                    cr.arc(w/2, h/2, min(w,h)/2 - 1, 0, 2*math.pi)
                    cr.set_line_width(1)
                    cr.stroke()
                return draw
            dot.set_draw_func(make_dot(r, g, b))
            btn.set_child(dot)
            btn.set_active(i == 0)
            self._color_buttons.append(btn)
            palette_box.append(btn)

        for i, (color, _) in enumerate(PALETTE):
            self._color_buttons[i].connect("toggled",
                self._make_color_handler(color, self._color_buttons[i]))

        right.append(palette_box)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(4); sep.set_margin_end(4)
        right.append(sep)

        # S / M / L size selector
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        size_box.add_css_class("linked")
        self._size_buttons = {}
        for label in ('S', 'M', 'L'):
            btn = Gtk.ToggleButton(label=label, tooltip_text=f"Size {label}")
            btn.set_active(label == DEFAULT_SIZE)
            self._size_buttons[label] = btn
            size_box.append(btn)
        for label, btn in self._size_buttons.items():
            btn.connect("toggled", self._make_size_handler(label, btn))
        right.append(size_box)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_start(4); sep2.set_margin_end(4)
        right.append(sep2)

        # Hamburger menu
        ham_menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("New Window",      "win.new-window")
        s2 = Gio.Menu()
        s2.append("Save",            "win.save")
        s2.append("Save As…",        "win.save-as")
        s2.append("Discard Changes", "win.discard")
        s3 = Gio.Menu()
        s3.append("About Annotator", "win.about")
        ham_menu.append_section(None, s1)
        ham_menu.append_section(None, s2)
        ham_menu.append_section(None, s3)
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_menu_model(ham_menu)
        menu_btn.set_tooltip_text("Menu")
        right.append(menu_btn)

        header.pack_end(right)
        return header

    def _make_tool_handler(self, mode):
        def on_toggle(btn, *_):
            if self._syncing:
                return
            self._syncing = True
            if btn.get_active():
                # Activate this tool, deactivate others
                for m, b in self._tool_buttons.items():
                    if m != mode:
                        b.set_active(False)
                self.canvas.tool_mode = mode
            else:
                # Button was toggled off — return to select mode
                self.canvas.tool_mode = 'select'
            self._syncing = False
            self.canvas.queue_draw()
        return on_toggle

    def sync_tool_button(self, mode: str):
        self._syncing = True
        for m, btn in self._tool_buttons.items():
            btn.set_active(m == mode)  # all off when mode == 'select'
        self._syncing = False

    def sync_toolbar(self, ann: 'Annotation'):
        """Update all toolbar controls to reflect ann's attributes (no side effects)."""
        self._syncing = True
        self.current_color = ann.color
        self.current_size  = ann.size
        for i, (color, _) in enumerate(PALETTE):
            self._color_buttons[i].set_active(color == ann.color)
        for lbl, btn in self._size_buttons.items():
            btn.set_active(lbl == ann.size)
        self._syncing = False

    def _make_color_handler(self, color, btn):
        def on_toggle(*_):
            if self._syncing:
                return
            self._syncing = True
            if btn.get_active():
                self.current_color = color
                if self.canvas.selected_ids:
                    self.canvas._push_undo(self.canvas._snapshot())
                    for aid in self.canvas.selected_ids:
                        ann = self.canvas._get_ann(aid)
                        if ann:
                            ann.color = color
                    self.canvas.queue_draw()
                for ob in self._color_buttons:
                    if ob is not btn:
                        ob.set_active(False)
            elif not any(ob.get_active() for ob in self._color_buttons):
                btn.set_active(True)
            self._syncing = False
        return on_toggle

    def _make_size_handler(self, label, btn):
        def on_toggle(*_):
            if self._syncing:
                return
            self._syncing = True
            if btn.get_active():
                self.current_size = label
                if self.canvas.selected_ids:
                    self.canvas._push_undo(self.canvas._snapshot())
                    for aid in self.canvas.selected_ids:
                        ann = self.canvas._get_ann(aid)
                        if ann:
                            ann.size = label
                            ann.box_w, ann.box_h = compute_box_size(ann.text, label)
                    self.canvas.queue_draw()
                for lbl, ob in self._size_buttons.items():
                    if lbl != label:
                        ob.set_active(False)
            elif not any(ob.get_active() for ob in self._size_buttons.values()):
                btn.set_active(True)
            self._syncing = False
        return on_toggle

    # ── File operations ──────────────────────────────────────────────────────

    def open_file(self):
        filt = Gtk.FileFilter()
        filt.set_name("Images")
        for mime in ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"):
            filt.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog = Gtk.FileDialog()
        dialog.set_title("Open Image")
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_open_done)

    def _on_open_done(self, dialog, result):
        try:
            path = dialog.open_finish(result).get_path()
            self.canvas.load_image(GdkPixbuf.Pixbuf.new_from_file(path))
            self._save_path = path
        except Exception as e:
            print(f"Open error: {e}")

    def paste_image(self):
        self.get_display().get_clipboard().read_texture_async(None, self._on_paste_done)

    def _on_paste_done(self, clipboard, result):
        try:
            texture = clipboard.read_texture_finish(result)
            if texture:
                import tempfile, os
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    tmp = f.name
                texture.save_to_png(tmp)
                self.canvas.load_image(GdkPixbuf.Pixbuf.new_from_file(tmp))
                os.unlink(tmp)
                self._save_path = None
        except Exception as e:
            print(f"Paste error: {e}")

    def new_window(self):
        AnnotatorWindow(self.get_application()).present()

    def save(self):
        if self._save_path:
            surf = self.canvas.render_to_surface()
            if surf:
                surf.write_to_png(self._save_path)
        else:
            self.save_as()

    def save_as(self):
        if not self.canvas.image:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Save As")
        dialog.set_initial_name("annotated.png")
        dialog.save(self, None, self._on_save_done)

    def _on_save_done(self, dialog, result):
        try:
            path = dialog.save_finish(result).get_path()
            surf = self.canvas.render_to_surface()
            if surf:
                self._save_path = path
                surf.write_to_png(path)
        except Exception as e:
            print(f"Save error: {e}")

    def discard_changes(self):
        self.canvas.annotations.clear()
        self.canvas.selected_ids = []
        self.canvas.undo_stack.clear()
        self.canvas.redo_stack.clear()
        self.canvas.queue_draw()

    def show_about(self):
        dialog = Adw.AboutDialog(
            application_name = "Annotator",
            developer_name = "Håvard Jakobsen",
            version          = VERSION,
            comments         = "A simple image annotation tool.",
            application_icon = "accessories-text-editor",
            license_type     = Gtk.License.MIT_X11,
        )
        dialog.present(self)

# ── App ───────────────────────────────────────────────────────────────────────

class AnnotatorApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.annotator")
        self.connect("activate", lambda app: AnnotatorWindow(app).present())

if __name__ == "__main__":
    sys.exit(AnnotatorApp().run(sys.argv))
