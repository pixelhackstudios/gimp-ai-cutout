#!/usr/bin/env python3
"""AI Cutout for GIMP 3.

Remove backgrounds, select subjects, select objects by clicking them, and refine
mask or selection edges using BiRefNet, ViTMatte and SAM 2.

The networks run in a separate local server (ai-cutout-server, started on demand
by systemd); this plug-in only moves pixels, so it needs nothing beyond the
Python standard library that GIMP ships.

Everything lives in the AI menu of the image window, with a "How to Use"
guide. The same commands also appear where GIMP users expect them: Select,
Layer > Transparency, Layer > Mask, and the Layers panel's right-click menu.
"""

import json
import math
import os
import sys
import threading
import urllib.error
import urllib.request

import gi
gi.require_version('Gimp', '3.0')
gi.require_version('Gegl', '0.4')
from gi.repository import Gimp, Gegl, GLib, GObject

SERVER = os.environ.get('AI_CUTOUT_URL', 'http://127.0.0.1:41879')
START_HINT = 'systemctl --user enable --now gimp-ai-cutout.socket'
PREVIEW_MAX = 640
RW = GObject.ParamFlags.READWRITE

PROC_REMOVE = 'python-fu-ai-cutout-remove-background'
PROC_SUBJECT = 'python-fu-ai-cutout-select-subject'
PROC_OBJECT = 'python-fu-ai-cutout-select-object'
PROC_REFINE_SEL = 'python-fu-ai-cutout-refine-selection'
PROC_REFINE_MASK = 'python-fu-ai-cutout-refine-mask'
PROC_GUIDE = 'python-fu-ai-cutout-guide'


class CutoutError(Exception):
    pass


# -- talking to the server ------------------------------------------------------

def request(path, params, body=b'', timeout=600):
    req = urllib.request.Request(SERVER + path, data=body, method='POST',
                                 headers={'X-Cutout': json.dumps(params),
                                          'Content-Type': 'application/octet-stream'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), json.loads(resp.headers.get('X-Cutout-Info') or 'null')
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get('error', str(e))
        except Exception:
            message = str(e)
        raise CutoutError('AI Cutout server error: ' + message)
    except (urllib.error.URLError, OSError) as e:
        raise CutoutError("AI Cutout's engine isn't running, so nothing was changed.\n\n"
                          'Open AI > How to Use AI Cutout to check it, or start it from a '
                          'terminal with:\n    ' + START_HINT)


def engine_status():
    """(ok, text) describing whether the AI engine answers. Starts it if it's asleep."""
    try:
        with urllib.request.urlopen(SERVER + '/health', timeout=30) as resp:
            info = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False, ("The AI engine isn't running. Re-run AI Cutout's install.sh, or start "
                       'it from a terminal with:\n' + START_HINT)
    where = 'graphics card' if info.get('device', '').startswith('cuda') else 'processor (slower)'
    return True, 'The AI engine is ready, running on your {}.'.format(where)


def request_with_progress(text, path, params, body=b''):
    """Run a request on a worker thread while pulsing GIMP's progress bar.

    The first request after the server starts also loads the networks onto the
    GPU, which takes a few seconds."""
    Gimp.progress_init(text)
    result = {}

    def work():
        try:
            result['value'] = request(path, params, body)
        except Exception as e:
            result['error'] = e

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(0.15)
        Gimp.progress_pulse()
    Gimp.progress_end()
    if 'error' in result:
        raise result['error']
    return result['value']


# -- pixels -------------------------------------------------------------------

def pixel_depth(image):
    return 'u8' if image.get_precision().value_nick.startswith('u8') else 'u16'


def read_rgba(drawable, region, depth):
    x, y, w, h = region
    return drawable.get_buffer().get(Gegl.Rectangle.new(x, y, w, h), 1.0,
                                     "R'G'B'A " + depth, Gegl.AbyssPolicy.CLAMP)


def read_gray16(drawable, region):
    # Masks and channels are stored linear ("Y"), so "Y u16" reads them unconverted.
    x, y, w, h = region
    return drawable.get_buffer().get(Gegl.Rectangle.new(x, y, w, h), 1.0,
                                     'Y u16', Gegl.AbyssPolicy.NONE)


def write(drawable, region, fmt, data):
    x, y, w, h = region
    buf = drawable.get_buffer()
    buf.set(Gegl.Rectangle.new(x, y, w, h), fmt, data)
    buf.flush()
    drawable.update(x, y, w, h)


# -- geometry (regions are (x, y, w, h) in layer coordinates) ------------------

def single_layer(drawables):
    if len(drawables) != 1:
        raise CutoutError('Select exactly one layer.')
    drawable = drawables[0]
    if isinstance(drawable, Gimp.LayerMask):
        drawable = Gimp.Layer.from_mask(drawable)
    if not isinstance(drawable, Gimp.Layer):
        raise CutoutError('AI Cutout works on layers, not channels.')
    return drawable


def offsets(layer):
    _, ox, oy = layer.get_offsets()
    return ox, oy


def canvas_region(image, layer):
    """The part of the layer that lies on the canvas."""
    ox, oy = offsets(layer)
    x0, y0 = max(0, -ox), max(0, -oy)
    x1 = min(layer.get_width(), image.get_width() - ox)
    y1 = min(layer.get_height(), image.get_height() - oy)
    if x1 <= x0 or y1 <= y0:
        raise CutoutError('The layer is entirely outside the canvas.')
    return x0, y0, x1 - x0, y1 - y0


def selection_region(image, layer, margin, within):
    """Selection bounds grown by `margin`, clipped to `within`; None if nothing is selected."""
    _, non_empty, sx1, sy1, sx2, sy2 = Gimp.Selection.bounds(image)
    if not non_empty:
        return None
    ox, oy = offsets(layer)
    wx, wy, ww, wh = within
    x0, y0 = max(wx, sx1 - ox - margin), max(wy, sy1 - oy - margin)
    x1, y1 = min(wx + ww, sx2 - ox + margin), min(wy + wh, sy2 - oy + margin)
    if x1 <= x0 or y1 <= y0:
        raise CutoutError('The selection does not overlap the active layer.')
    return x0, y0, x1 - x0, y1 - y0


def auto_margin(w, h, band):
    return 2 * (band or max(8, round(max(w, h) / 100))) + 32


# -- writing results ------------------------------------------------------------

def set_layer_mask(layer, region, alpha16):
    """Give `layer` a fresh mask: `alpha16` inside `region`, black outside.

    Replacing the mask (rather than painting into it) keeps the whole change
    inside one undo step."""
    if layer.get_mask() is not None:
        layer.remove_mask(Gimp.MaskApplyMode.DISCARD)
    mask = layer.create_mask(Gimp.AddMaskType.BLACK)
    layer.add_mask(mask)
    write(mask, region, 'Y u16', alpha16)
    return mask


def set_selection(image, layer, region, alpha16, keep_outside=False):
    """Replace the selection with `alpha16` placed at `region` (layer coordinates)."""
    ox, oy = offsets(layer)
    if keep_outside:
        channel = Gimp.Selection.save(image)
    else:
        channel = Gimp.Channel.new(image, 'AI Cutout', image.get_width(), image.get_height(),
                                   100.0, Gegl.Color.new('black'))
        image.insert_channel(channel, None, 0)
        write(channel, (0, 0, image.get_width(), image.get_height()), 'Y u8',
              bytes(image.get_width() * image.get_height()))
    x, y, w, h = region
    write(channel, (x + ox, y + oy, w, h), 'Y u16', alpha16)
    image.select_item(Gimp.ChannelOps.REPLACE, channel)
    image.remove_channel(channel)


def add_cutout_layer(image, layer, region, alpha16, rgba):
    """Decontaminated copy of `layer` above it, masked; the original is hidden, not changed."""
    cutout = layer.copy()
    image.insert_layer(cutout, layer.get_parent(), image.get_item_position(layer))
    cutout.set_name(layer.get_name() + ' (cutout)')
    if not cutout.has_alpha():
        cutout.add_alpha()
    write(cutout, region, "R'G'B'A " + pixel_depth(image), rgba)
    set_layer_mask(cutout, region, alpha16)
    layer.set_visible(False)
    image.set_selected_layers([cutout])


# -- dialogs ------------------------------------------------------------------

def run_procedure_dialog(procedure, config, title, ok_label):
    gi.require_version('GimpUi', '3.0')
    from gi.repository import GimpUi
    GimpUi.init(procedure.get_name())
    dialog = GimpUi.ProcedureDialog(procedure=procedure, config=config, title=title)
    dialog.set_ok_label(ok_label)
    dialog.fill(None)
    accepted = dialog.run()
    dialog.destroy()
    return accepted


def parse_points(text):
    points = []
    for item in (text or '').replace(';', ' ').split():
        x, y = item.split(',')
        points.append([float(x), float(y)])
    return points


def format_points(points):
    return ' '.join('{:.1f},{:.1f}'.format(x, y) for x, y in points)


class ObjectPicker:
    """Click-to-select dialog: left-click adds the object, right/Shift-click excludes."""

    def __init__(self, layer, region, session, box, config):
        gi.require_version('GimpUi', '3.0')
        gi.require_version('Gtk', '3.0')
        gi.require_version('Gdk', '3.0')
        gi.require_version('GdkPixbuf', '2.0')
        gi.require_version('Pango', '1.0')
        from gi.repository import GimpUi, Gtk, Gdk, GdkPixbuf, Pango
        self.Gtk, self.Gdk, self.GdkPixbuf = Gtk, Gdk, GdkPixbuf

        GimpUi.init(PROC_OBJECT)
        self.session, self.box = session, box
        self.points, self.labels = [], []
        x, y, w, h = region
        self.scale = min(1.0, PREVIEW_MAX / max(w, h))
        self.pw, self.ph = max(1, int(w * self.scale)), max(1, int(h * self.scale))
        preview = layer.get_buffer().get(
            Gegl.Rectangle.new(int(x * self.scale), int(y * self.scale), self.pw, self.ph),
            self.scale, "R'G'B'A u8", Gegl.AbyssPolicy.CLAMP)
        self.base = self._pixbuf(preview)
        self.overlay = None

        use_header_bar = Gtk.Settings.get_default().get_property('gtk-dialogs-use-header')
        self.dialog = GimpUi.Dialog(use_header_bar=use_header_bar, title='Select Object by Clicking',
                                    role='ai-cutout-object')
        GimpUi.window_set_transient(self.dialog)
        self.dialog.add_button('_Cancel', Gtk.ResponseType.CANCEL)
        self.ok_button = self.dialog.add_button('_Select', Gtk.ResponseType.OK)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_border_width(12)
        self.dialog.get_content_area().add(vbox)
        vbox.pack_start(GimpUi.HintBox.new(
            'Click the object you want. Right-click or Shift-click marks areas to leave out.'),
            False, False, 0)

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(self.pw, self.ph)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.area.connect('draw', self.on_draw)
        self.area.connect('button-press-event', self.on_press)
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.set_halign(Gtk.Align.CENTER)
        frame.add(self.area)
        vbox.pack_start(frame, True, True, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.status = Gtk.Label(xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        row.pack_start(self.status, True, True, 0)
        undo = Gtk.Button.new_with_mnemonic('_Undo point')
        undo.connect('clicked', self.on_undo)
        clear = Gtk.Button.new_with_mnemonic('C_lear')
        clear.connect('clicked', self.on_clear)
        row.pack_end(clear, False, False, 0)
        row.pack_end(undo, False, False, 0)
        vbox.pack_start(row, False, False, 0)

        options = Gtk.Grid(column_spacing=12, row_spacing=6)
        options.attach(Gtk.Label(label='Output:', xalign=0), 0, 0, 1, 1)
        self.output = Gtk.ComboBoxText()
        self.output.append('selection', 'Selection')
        self.output.append('mask', 'Layer mask')
        self.output.set_active_id(config.get_property('output'))
        options.attach(self.output, 1, 0, 1, 1)
        self.refine = Gtk.CheckButton.new_with_mnemonic('Refine hair and soft ed_ges')
        self.refine.set_active(config.get_property('refine-edges'))
        options.attach(self.refine, 2, 0, 1, 1)
        options.attach(Gtk.Label(label='Edge width (0 = auto):', xalign=0), 0, 1, 1, 1)
        self.band = Gtk.SpinButton.new_with_range(0, 1000, 1)
        self.band.set_value(config.get_property('edge-width'))
        options.attach(self.band, 1, 1, 1, 1)
        vbox.pack_start(options, False, False, 0)

        self.dialog.show_all()
        self.refresh()

    def _pixbuf(self, rgba):
        return self.GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(rgba), self.GdkPixbuf.Colorspace.RGB, True, 8, self.pw, self.ph, self.pw * 4)

    def run(self):
        response = self.dialog.run()
        result = None
        if response == self.Gtk.ResponseType.OK:
            result = {'output': self.output.get_active_id(), 'refine': self.refine.get_active(),
                      'band': int(self.band.get_value())}
        self.dialog.destroy()
        return result

    def refresh(self):
        has_prompt = bool(self.points or self.box)
        self.ok_button.set_sensitive(has_prompt)
        if not has_prompt:
            self.overlay = None
            self.status.set_text('No object selected yet.')
        else:
            try:
                data, info = request('/v1/sam/predict', {
                    'session': self.session, 'points': self.points, 'labels': self.labels,
                    'box': self.box, 'preview': [self.pw, self.ph]})
                self.overlay = self._pixbuf(data)
                kept = sum(self.labels)
                self.status.set_text('{} object point(s), {} excluded. Confidence {:.0%}.'.format(
                    kept, len(self.labels) - kept, info['score']))
            except CutoutError as e:
                self.status.set_text(str(e).splitlines()[0])
        self.area.queue_draw()

    def on_draw(self, widget, cr):
        cr.set_source_rgb(0.4, 0.4, 0.4)
        cr.paint()
        self.Gdk.cairo_set_source_pixbuf(cr, self.base, 0, 0)
        cr.paint()
        if self.overlay is not None:
            self.Gdk.cairo_set_source_pixbuf(cr, self.overlay, 0, 0)
            cr.paint()
        if self.box:
            x0, y0, x1, y1 = (v * self.scale for v in self.box)
            cr.set_source_rgb(1, 0.85, 0.2)
            cr.set_line_width(1.5)
            cr.set_dash([6, 4])
            cr.rectangle(x0, y0, x1 - x0, y1 - y0)
            cr.stroke()
            cr.set_dash([])
        for (px, py), label in zip(self.points, self.labels):
            cr.arc(px * self.scale, py * self.scale, 6, 0, 2 * math.pi)
            cr.set_source_rgb(*((0.2, 0.8, 0.3) if label else (0.9, 0.2, 0.2)))
            cr.fill_preserve()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(2)
            cr.stroke()
        return False

    def on_press(self, widget, event):
        if event.type != self.Gdk.EventType.BUTTON_PRESS or event.button not in (1, 3):
            return False
        include = event.button == 1 and not (event.state & self.Gdk.ModifierType.SHIFT_MASK)
        self.points.append([event.x / self.scale, event.y / self.scale])
        self.labels.append(1 if include else 0)
        self.refresh()
        return True

    def on_undo(self, button):
        if self.points:
            self.points.pop()
            self.labels.pop()
            self.refresh()

    def on_clear(self, button):
        self.points, self.labels, self.box = [], [], None
        self.refresh()


# -- procedures ---------------------------------------------------------------

def remove_background(procedure, run_mode, image, layer, config):
    if run_mode == Gimp.RunMode.INTERACTIVE and not run_procedure_dialog(
            procedure, config, 'Remove Background', 'Remove _Background'):
        return False
    refine, band = config.get_property('refine-edges'), config.get_property('edge-width')
    decontaminate = config.get_property('decontaminate')
    region = (0, 0, layer.get_width(), layer.get_height())
    if config.get_property('limit-to-selection'):
        _, non_empty, sx1, sy1, sx2, sy2 = Gimp.Selection.bounds(image)
        if non_empty:
            margin = round(0.05 * max(sx2 - sx1, sy2 - sy1)) + 16
            region = selection_region(image, layer, margin, region)
    depth = pixel_depth(image)
    out, _ = request_with_progress('Removing background', '/v1/matte', {
        'width': region[2], 'height': region[3], 'depth': depth, 'matting': refine,
        'band': band, 'foreground': decontaminate}, read_rgba(layer, region, depth))
    split = region[2] * region[3] * 2
    image.undo_group_start()
    try:
        if decontaminate:
            add_cutout_layer(image, layer, region, out[:split], out[split:])
        else:
            set_layer_mask(layer, region, out[:split])
    finally:
        image.undo_group_end()
    return True


def select_subject(procedure, run_mode, image, layer, config):
    region = canvas_region(image, layer)
    depth = pixel_depth(image)
    out, _ = request_with_progress('Selecting subject', '/v1/matte', {
        'width': region[2], 'height': region[3], 'depth': depth,
        'matting': config.get_property('refine-edges'), 'band': config.get_property('edge-width')},
        read_rgba(layer, region, depth))
    image.undo_group_start()
    try:
        set_selection(image, layer, region, out)
    finally:
        image.undo_group_end()
    return True


def refine_selection(procedure, run_mode, image, layer, config):
    if Gimp.Selection.is_empty(image):
        raise CutoutError('Make a rough selection first (any selection tool), then refine it.')
    if run_mode == Gimp.RunMode.INTERACTIVE and not run_procedure_dialog(
            procedure, config, 'Refine Selection Edges', 'R_efine'):
        return False
    band = config.get_property('edge-width')
    _, _, sx1, sy1, sx2, sy2 = Gimp.Selection.bounds(image)
    region = selection_region(image, layer, auto_margin(sx2 - sx1, sy2 - sy1, band),
                              canvas_region(image, layer))
    ox, oy = offsets(layer)
    x, y, w, h = region
    selection = read_gray16(image.get_selection(), (x + ox, y + oy, w, h))
    depth = pixel_depth(image)
    out, _ = request_with_progress('Refining selection edges', '/v1/matte', {
        'width': w, 'height': h, 'depth': depth, 'refine': True, 'band': band},
        read_rgba(layer, region, depth) + selection)
    image.undo_group_start()
    try:
        set_selection(image, layer, region, out, keep_outside=True)
    finally:
        image.undo_group_end()
    return True


def refine_mask(procedure, run_mode, image, layer, config):
    mask = layer.get_mask()
    if mask is None:
        raise CutoutError('The layer has no mask. Add one (Layer > Mask > Add Layer Mask), '
                          'paint it roughly, then refine it.')
    if run_mode == Gimp.RunMode.INTERACTIVE and not run_procedure_dialog(
            procedure, config, 'Refine Layer Mask Edges', 'R_efine'):
        return False
    region = (0, 0, layer.get_width(), layer.get_height())
    depth = pixel_depth(image)
    out, _ = request_with_progress('Refining mask edges', '/v1/matte', {
        'width': region[2], 'height': region[3], 'depth': depth, 'refine': True,
        'band': config.get_property('edge-width')},
        read_rgba(layer, region, depth) + read_gray16(mask, region))
    image.undo_group_start()
    try:
        set_layer_mask(layer, region, out)
    finally:
        image.undo_group_end()
    return True


def select_object(procedure, run_mode, image, layer, config):
    region = canvas_region(image, layer)
    x, y, w, h = region
    box = None
    _, non_empty, sx1, sy1, sx2, sy2 = Gimp.Selection.bounds(image)
    if non_empty:
        ox, oy = offsets(layer)
        box = [max(0, sx1 - ox - x), max(0, sy1 - oy - y), min(w, sx2 - ox - x), min(h, sy2 - oy - y)]
        if box[2] <= box[0] or box[3] <= box[1]:
            box = None
    depth = pixel_depth(image)
    raw, _ = request_with_progress('Analysing image', '/v1/sam/session', {
        'width': w, 'height': h, 'depth': depth}, read_rgba(layer, region, depth))
    session = json.loads(raw)['session']
    try:
        if run_mode == Gimp.RunMode.INTERACTIVE:
            picker = ObjectPicker(layer, region, session, box, config)
            choice = picker.run()
            if choice is None:
                return False
            points, labels, box = picker.points, picker.labels, picker.box
            config.set_property('output', choice['output'])
            config.set_property('refine-edges', choice['refine'])
            config.set_property('edge-width', choice['band'])
            # Remember the clicks in layer coordinates so the call can be repeated by script.
            in_layer = [((px + x, py + y), l) for (px, py), l in zip(points, labels)]
            config.set_property('points', format_points(p for p, l in in_layer if l))
            config.set_property('exclude', format_points(p for p, l in in_layer if not l))
        else:
            include = parse_points(config.get_property('points'))
            exclude = parse_points(config.get_property('exclude'))
            points = [[px - x, py - y] for px, py in include + exclude]
            labels = [1] * len(include) + [0] * len(exclude)
            if not points and box is None:
                raise CutoutError('Give at least one point on the object, or a selection around it.')
        out, _ = request_with_progress('Selecting object', '/v1/sam/apply', {
            'session': session, 'points': points, 'labels': labels, 'box': box,
            'matting': config.get_property('refine-edges'), 'band': config.get_property('edge-width')})
    finally:
        try:
            request('/v1/sam/close', {'session': session}, timeout=10)
        except CutoutError:
            pass
    image.undo_group_start()
    try:
        if config.get_property('output') == 'mask':
            set_layer_mask(layer, region, out)
        else:
            set_selection(image, layer, region, out)
    finally:
        image.undo_group_end()
    return True


RUNNERS = {
    PROC_REMOVE: remove_background,
    PROC_SUBJECT: select_subject,
    PROC_OBJECT: select_object,
    PROC_REFINE_SEL: refine_selection,
    PROC_REFINE_MASK: refine_mask,
}


def run(procedure, run_mode, image, drawables, config, data):
    try:
        layer = single_layer(drawables)
        done = RUNNERS[procedure.get_name()](procedure, run_mode, image, layer, config)
    except CutoutError as e:
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(e)))
    if not done:
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    Gimp.displays_flush()
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


GUIDE = """<span size="large"><b>AI Cutout</b></span>
Everything is in the <b>AI</b> menu at the top of the image window.

<b>Remove Background...</b>
Cuts the main subject out of the current layer. Nothing is deleted: the background
is hidden with a layer mask. Paint the mask white to bring parts back, black to hide more.
Also on the Layers panel: right-click a layer.

<b>Select Subject</b>
Selects the main subject of the layer in one click.

<b>Select Object by Clicking...</b>
Shows your image. Click the thing you want; right-click anything that shouldn't be included.

<b>Refine Selection Edges...</b>
Draw a rough selection around something with hair or fur, then run this to get the fine edges.

<b>Refine Layer Mask Edges...</b>
The same, for a layer mask you painted roughly.

<b>Tips</b>
  - Ctrl+Z undoes any of these in one step.
  - Press / anywhere in GIMP and type "background" or "subject" to find these commands.
  - The first use after starting your computer takes a few seconds while the AI wakes up."""


def run_guide(procedure, run_mode, image, drawables, config, data):
    if run_mode != Gimp.RunMode.INTERACTIVE:
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    gi.require_version('GimpUi', '3.0')
    gi.require_version('Gtk', '3.0')
    from gi.repository import GimpUi, Gtk
    GimpUi.init(PROC_GUIDE)
    use_header_bar = Gtk.Settings.get_default().get_property('gtk-dialogs-use-header')
    dialog = GimpUi.Dialog(use_header_bar=use_header_bar, title='How to Use AI Cutout',
                           role='ai-cutout-guide')
    GimpUi.window_set_transient(dialog)
    dialog.add_button('_Close', Gtk.ResponseType.CLOSE)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.set_border_width(16)
    dialog.get_content_area().add(box)
    guide = Gtk.Label(xalign=0)
    guide.set_markup(GUIDE)
    box.pack_start(guide, False, False, 0)
    ok, text = engine_status()
    status = Gtk.Label(xalign=0, selectable=True)
    status.set_markup('<b>{}</b> {}'.format('✔' if ok else '✖', GLib.markup_escape_text(text)))
    box.pack_start(status, False, False, 0)
    dialog.show_all()
    dialog.run()
    dialog.destroy()
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


def add_edge_arguments(procedure, refine=True):
    if refine:
        procedure.add_boolean_argument(
            'refine-edges', 'Refine hair and soft ed_ges',
            'Run the matting network on the edge band (slower; recovers hair, fur and soft edges)',
            True, RW)
    procedure.add_int_argument(
        'edge-width', 'Edge _width (0 = auto)',
        'Half-width in pixels of the uncertain band along the edge; widen it for long hair or fur',
        0, 1000, 0, RW)


AI_MENU = '<Image>/_AI'

# name: (menu label, extra places it also appears, short help, long help)
COMMANDS = {
    PROC_REMOVE: (
        'Remove Background...', ['<Image>/Layer/Transparency', '<Layers>/Layers Menu'],
        'Cut out the main subject (hides the background with a layer mask)',
        'Finds the main subject with BiRefNet, refines its edges with ViTMatte and adds a '
        'layer mask. Nothing is deleted; paint on the mask to fix mistakes.'),
    PROC_SUBJECT: (
        'Select Subject', ['<Image>/Select'],
        'Select the main subject of the layer',
        'Finds the main subject with BiRefNet and selects it, with soft edges from ViTMatte.'),
    PROC_OBJECT: (
        'Select Object by Clicking...', ['<Image>/Select'],
        'Select any object by clicking on it',
        'Click the object in the preview; SAM 2 segments it and ViTMatte refines its edges. '
        'A selection, if present, is used as a box around the object. Non-interactively, '
        'pass points as "x,y x,y" in layer coordinates.'),
    PROC_REFINE_SEL: (
        'Refine Selection Edges...', ['<Image>/Select'],
        'Turn a rough selection into a precise one (hair, fur, soft edges)',
        'Keeps the inside and outside of the current selection and works out hair, fur and '
        'soft edges along its border with ViTMatte.'),
    PROC_REFINE_MASK: (
        'Refine Layer Mask Edges...', ['<Image>/Layer/Mask'],
        'Turn a rough layer mask into a precise one (hair, fur, soft edges)',
        'Keeps the inside and outside of the layer mask and works out hair, fur and soft '
        'edges along its border with ViTMatte.'),
}


class AICutout(Gimp.PlugIn):
    def do_set_i18n(self, name):
        return False

    def do_query_procedures(self):
        return list(RUNNERS) + [PROC_GUIDE]

    def do_create_procedure(self, name):
        if name == PROC_GUIDE:
            procedure = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, run_guide, None)
            procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.ALWAYS |
                                           Gimp.ProcedureSensitivityMask.NO_IMAGE)
            procedure.set_menu_label('How to Use AI Cutout...')
            procedure.add_menu_path(AI_MENU)
            procedure.add_menu_path('<Image>/Help')
            procedure.set_documentation('How to use AI Cutout, and whether its AI engine is running',
                                        '', name)
            procedure.set_attribution('AI Cutout', 'AI Cutout', '2026')
            return procedure

        label, also_in, blurb, help_text = COMMANDS[name]
        procedure = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, run, None)
        procedure.set_image_types('RGB*, GRAY*')
        procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE)
        procedure.set_attribution('AI Cutout', 'AI Cutout (BiRefNet, ViTMatte, SAM 2)', '2026')
        procedure.set_menu_label(label)
        procedure.set_documentation(blurb, help_text, name)
        for path in [AI_MENU] + also_in:
            procedure.add_menu_path(path)

        if name == PROC_REMOVE:
            add_edge_arguments(procedure)
            procedure.add_boolean_argument(
                'decontaminate', '_Decontaminate colors (new layer)',
                'Put the cutout on a copy of the layer with background colour removed from edge pixels',
                False, RW)
            procedure.add_boolean_argument(
                'limit-to-selection', 'Look only around the selectio_n',
                'If something is selected, only look for the subject in and around it', True, RW)
        elif name == PROC_SUBJECT:
            add_edge_arguments(procedure)
        elif name == PROC_OBJECT:
            choice = Gimp.Choice.new()
            choice.add('selection', 0, 'Selection', '')
            choice.add('mask', 1, 'Layer mask', '')
            procedure.add_choice_argument('output', 'Out_put', 'Where to put the result',
                                          choice, 'selection', RW)
            add_edge_arguments(procedure)
            procedure.add_string_argument('points', 'Object points',
                                          'Points on the object, "x,y x,y" (layer coordinates)', '', RW)
            procedure.add_string_argument('exclude', 'Excluded points',
                                          'Points not on the object, "x,y x,y" (layer coordinates)', '', RW)
        else:
            add_edge_arguments(procedure, refine=False)
        return procedure


Gimp.main(AICutout.__gtype__, sys.argv)
