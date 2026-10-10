"""End-to-end test of the installed plug-in, run inside a headless GIMP.

    tests/run_gimp_e2e.sh IMAGE OUTDIR

Calls every AI Cutout procedure non-interactively through the PDB and checks
what lands in the image (masks, selections, layers). Writes PNGs to OUTDIR for
eyeballing and a report to OUTDIR/report.txt.
"""

import os
import traceback

import gi
gi.require_version('Gimp', '3.0')
gi.require_version('Gegl', '0.4')
from gi.repository import Gimp, Gegl, Gio

SRC = os.environ['E2E_IMAGE']
OUT = os.environ['E2E_OUT']
report = []
failures = 0


def log(*a):
    report.append(' '.join(str(x) for x in a))


def check(cond, what):
    global failures
    log(('PASS ' if cond else 'FAIL ') + what)
    failures += 0 if cond else 1


def call(name, image, layer, **props):
    proc = Gimp.get_pdb().lookup_procedure(name)
    cfg = proc.create_config()
    cfg.set_property('run-mode', Gimp.RunMode.NONINTERACTIVE)
    cfg.set_property('image', image)
    cfg.set_core_object_array('drawables', [layer] if layer else [])
    for k, v in props.items():
        cfg.set_property(k.replace('_', '-'), v)
    res = proc.run(cfg)
    status = res.index(0)
    if status != Gimp.PDBStatusType.SUCCESS:
        return status, res.index(1) if res.length() > 1 else ''
    return status, ''


def gray_stats(drawable, x, y, w, h):
    data = drawable.get_buffer().get(Gegl.Rectangle.new(x, y, w, h), 1.0, 'Y u8', Gegl.AbyssPolicy.NONE)
    n = len(data)
    opaque = sum(1 for b in data if b == 255)
    clear = sum(1 for b in data if b == 0)
    return opaque / n, clear / n, (n - opaque - clear) / n


def load():
    img = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(SRC))
    return img, img.get_layers()[0]


def export(image, name):
    flat = image.duplicate()
    flat.merge_visible_layers(Gimp.MergeType.CLIP_TO_IMAGE)
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, flat, Gio.File.new_for_path(os.path.join(OUT, name)), None)
    flat.delete()


def selection_to_layer(image, name):
    """Export the selection as a greyscale PNG (white = selected)."""
    w, h = image.get_width(), image.get_height()
    rect = Gegl.Rectangle.new(0, 0, w, h)
    vis = Gimp.Image.new(w, h, Gimp.ImageBaseType.GRAY)
    lay = Gimp.Layer.new(vis, 'selection', w, h, Gimp.ImageType.GRAY_IMAGE, 100, Gimp.LayerMode.NORMAL)
    vis.insert_layer(lay, None, 0)
    data = image.get_selection().get_buffer().get(rect, 1.0, 'Y u8', Gegl.AbyssPolicy.NONE)
    buf = lay.get_buffer()
    buf.set(rect, "Y' u8", data)  # store alpha values as-is for viewing
    buf.flush()
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, vis, Gio.File.new_for_path(os.path.join(OUT, name)), None)
    vis.delete()


try:
    # 1. Remove background -> layer mask
    img, layer = load()
    w, h = layer.get_width(), layer.get_height()
    status, err = call('python-fu-ai-cutout-remove-background', img, layer)
    check(status == Gimp.PDBStatusType.SUCCESS, f'remove-background ran ({err})')
    mask = layer.get_mask()
    check(mask is not None, 'remove-background added a layer mask')
    if mask:
        o, c, s = gray_stats(mask, 0, 0, w, h)
        log(f'     mask: {o:.0%} opaque, {c:.0%} clear, {s:.1%} soft')
        check(o > 0.05 and c > 0.05 and s > 0.001, 'mask has subject, background and soft edges')
    export(img, '1_remove_background.png')

    # Running it again on a layer that already has a mask replaces the mask.
    status, err = call('python-fu-ai-cutout-remove-background', img, layer, refine_edges=False)
    check(status == Gimp.PDBStatusType.SUCCESS and layer.get_mask() is not None, f'remove-background twice ({err})')
    img.delete()

    # 2. Decontaminate -> new layer above, original hidden
    img, layer = load()
    status, err = call('python-fu-ai-cutout-remove-background', img, layer, decontaminate=True)
    layers = img.get_layers()
    check(status == Gimp.PDBStatusType.SUCCESS and len(layers) == 2, f'decontaminate made a new layer ({err})')
    if len(layers) == 2:
        check(layers[0].get_mask() is not None and not layers[1].get_visible() and layers[1].get_mask() is None,
              'new layer is masked, original hidden and untouched')
        check(layers[0].get_name().endswith('(cutout)'), 'new layer named "... (cutout)"')
    export(img, '2_decontaminated.png')
    img.delete()

    # 3. Select subject -> selection; layer offset partly off-canvas
    img, layer = load()
    layer.set_offsets(-w // 4, 0)
    status, err = call('python-fu-ai-cutout-select-subject', img, layer)
    check(status == Gimp.PDBStatusType.SUCCESS, f'select-subject ran with off-canvas layer ({err})')
    _, non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(img)
    log(f'     selection bounds {x1},{y1} {x2},{y2}')
    check(non_empty and x2 - x1 < img.get_width(), 'select-subject produced a partial selection')
    check(len(img.get_channels()) == 0, 'no temporary channels left behind')
    selection_to_layer(img, '3_select_subject.png')
    img.delete()

    # 4. Refine edges of a rough rectangle selection
    img, layer = load()
    img.select_ellipse(Gimp.ChannelOps.REPLACE, w * 0.2, h * 0.1, w * 0.6, h * 0.85)
    status, err = call('python-fu-ai-cutout-refine-selection', img, layer, edge_width=30)
    check(status == Gimp.PDBStatusType.SUCCESS, f'refine-selection ran ({err})')
    o, c, s = gray_stats(img.get_selection(), 0, 0, img.get_width(), img.get_height())
    log(f'     selection: {o:.0%} full, {c:.0%} empty, {s:.1%} partial')
    check(s > 0.001, 'refined selection has soft edges')
    check(len(img.get_channels()) == 0, 'no temporary channels left behind')
    selection_to_layer(img, '4_refine_selection.png')
    img.delete()

    # 5. Refine a rough hand-made mask
    img, layer = load()
    img.select_ellipse(Gimp.ChannelOps.REPLACE, w * 0.2, h * 0.1, w * 0.6, h * 0.85)
    layer.add_mask(layer.create_mask(Gimp.AddMaskType.SELECTION))
    Gimp.Selection.none(img)
    status, err = call('python-fu-ai-cutout-refine-mask', img, layer, edge_width=30)
    check(status == Gimp.PDBStatusType.SUCCESS, f'refine-mask ran ({err})')
    o, c, s = gray_stats(layer.get_mask(), 0, 0, w, h)
    log(f'     mask: {o:.0%} opaque, {c:.0%} clear, {s:.1%} soft')
    check(s > 0.001, 'refined mask has soft edges')
    export(img, '5_refine_mask.png')
    img.delete()

    # refine-mask without a mask must fail politely
    img, layer = load()
    status, err = call('python-fu-ai-cutout-refine-mask', img, layer)
    check(status == Gimp.PDBStatusType.EXECUTION_ERROR and 'no mask' in str(err), f'refine-mask without mask errors: {err}')
    img.delete()

    # 6. Select object by points (SAM 2) -> selection, then -> layer mask
    img, layer = load()
    pts = os.environ.get('E2E_POINTS') or f'{w // 2},{h // 2}'
    status, err = call('python-fu-ai-cutout-select-object', img, layer, points=pts)
    check(status == Gimp.PDBStatusType.SUCCESS and not Gimp.Selection.is_empty(img),
          f'select-object (points {pts}) made a selection ({err})')
    selection_to_layer(img, '6_select_object.png')
    Gimp.Selection.none(img)
    status, err = call('python-fu-ai-cutout-select-object', img, layer, points=pts, output='mask')
    check(status == Gimp.PDBStatusType.SUCCESS and layer.get_mask() is not None,
          f'select-object output=mask added a mask ({err})')
    export(img, '6_select_object_mask.png')
    img.delete()

    # 7. 16-bit image goes through the u16 path
    img, layer = load()
    img.convert_precision(Gimp.Precision.U16_NON_LINEAR)
    status, err = call('python-fu-ai-cutout-remove-background', img, layer, decontaminate=True)
    check(status == Gimp.PDBStatusType.SUCCESS, f'16-bit remove-background + decontaminate ({err})')
    img.delete()

    # 8. Remove background limited to a selection
    img, layer = load()
    img.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, w // 2, h)
    status, err = call('python-fu-ai-cutout-remove-background', img, layer, limit_to_selection=True)
    check(status == Gimp.PDBStatusType.SUCCESS, f'remove-background limited to selection ({err})')
    if layer.get_mask():
        o, c, s = gray_stats(layer.get_mask(), w - w // 8, 0, w // 8, h)
        check(o == 0, 'mask is empty far outside the selection')
    img.delete()

    # 9. Generate Image (Z-Image-Turbo through the server)
    prompt = 'a red apple on a wooden table, soft daylight, photo'
    img, layer = load()
    img.select_rectangle(Gimp.ChannelOps.REPLACE, 40, 30, 300, 200)
    status, err = call('python-fu-ai-cutout-generate', img, layer, prompt=prompt, seed=3)
    layers = img.get_layers()
    check(status == Gimp.PDBStatusType.SUCCESS and len(layers) == 2, f'generate into a selection ({err})')
    if len(layers) == 2:
        gen = layers[0]
        check((gen.get_width(), gen.get_height()) == (300, 200) and tuple(gen.get_offsets()[1:]) == (40, 30),
              f'generated layer fills the selection: {gen.get_width()}x{gen.get_height()} at {gen.get_offsets()[1:]}')
        check(gen.get_name().startswith('AI: a red apple'), f'generated layer is named after the prompt: {gen.get_name()!r}')
        px = gen.get_buffer().get(Gegl.Rectangle.new(0, 0, 300, 200), 1.0, "R'G'B'A u8", Gegl.AbyssPolicy.NONE)
        check(max(px) - min(px) > 100, 'generated layer has real picture content')
    export(img, '9_generate_selection.png')
    img.delete()

    img, layer = load()
    status, err = call('python-fu-ai-cutout-generate', img, layer, prompt=prompt, shape='square', seed=3)
    gen = img.get_layers()[0]
    check(status == Gimp.PDBStatusType.SUCCESS and (gen.get_width(), gen.get_height()) == (1024, 1024)
          and tuple(gen.get_offsets()[1:]) == ((w - 1024) // 2, (h - 1024) // 2),
          f'square generation is centred at full size ({err})')
    status, err = call('python-fu-ai-cutout-generate', img, layer, prompt='   ')
    check(status == Gimp.PDBStatusType.EXECUTION_ERROR and 'Describe' in str(err), f'empty prompt errors: {err}')
    img.delete()

    before = {i.get_id() for i in Gimp.get_images()}
    status, err = call('python-fu-ai-cutout-generate', None, None, prompt=prompt, seed=3)
    created = [i for i in Gimp.get_images() if i.get_id() not in before]
    check(status == Gimp.PDBStatusType.SUCCESS and len(created) == 1, f'generate with no image open makes a new image ({err})')
    if len(created) == 1:
        new = created[0]
        check((new.get_width(), new.get_height()) == (1024, 1024), f'new image is 1024x1024: {new.get_width()}x{new.get_height()}')
        export(new, '9_generate_new_image.png')
except Exception:
    failures += 1
    log(traceback.format_exc())

log(f'\n{failures} failure(s)')
with open(os.path.join(OUT, 'report.txt'), 'w') as f:
    f.write('\n'.join(report) + '\n')
