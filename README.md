# Annotator

A GTK4 image annotation tool. Load an image, add callouts, labels, and arrows, then save the result as a PNG.

![Example](/resources/example.png)

## Requirements

- Python 3.10+
- GTK 4, libadwaita, PyGObject, Cairo

On Fedora:

```
sudo dnf install python3-gobject gtk4 libadwaita pango cairo-gobject
```

On Ubuntu/Debian:

```
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0 python3-gi-cairo
```

To add a desktop shortcut:

- Edit the path to `annotator.py` in `/resources/annotator.desktop`
- Copy `/resources/annotator.desktop` to `~/.local/share/applications`

## Running

```
python annotator.py
```

## Workflow

1. Open an image with Ctrl+O, drag-and-drop, or paste from clipboard (Ctrl+V).
2. Select a tool from the toolbar: **Callout**, **Label**, or **Arrow**.
3. Click on the image to place an annotation and start typing.
4. Save with Ctrl+S. The output is a flat PNG with annotations baked in.

## Tools

- **Callout:** text box with a pointer arrow. Drag the arrowhead to reposition it.
- **Label:** text box without an arrow.
- **Arrow:** freestanding arrow. Click and drag to set direction and length.

Click a tool button again to return to Select mode.

## Text editing

- Click an empty area to place an annotation and enter edit mode.
- Double-click an existing annotation to edit it.
- Shift+Enter for a newline, Enter to commit, Escape to cancel.

## Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+O | Open image |
| Ctrl+N | New window |
| Ctrl+S | Save |
| Ctrl+Shift+S | Save As |
| Ctrl+Z | Undo |
| Ctrl+Y | Redo |
| Ctrl+X / C / V | Cut / Copy / Paste |
| Ctrl+A | Select all |
| Delete | Delete selected |
| Escape | Cancel / deselect / exit tool |

## License

MIT
