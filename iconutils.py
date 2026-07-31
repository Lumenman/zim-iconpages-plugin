# -*- coding: utf-8 -*-

# Released under the GNU GPL version 3.
# This is a plugin for the Zim desktop wiki (zim-wiki.org).

'''Loading and caching of the icons used by the IconPages plugin, and the
helpers to read/write the "[ICON=name]" shortcode that is stored in the
wiki text.

SVG, PNG and ICO files are supported, as far as the local GdkPixbuf
can decode them (SVG needs the librsvg loader; see
L{_get_supported_extensions}).

Icons are *not* installed into Zim's own data folder. They ship inside
this plugin's own folder (in the "icons" sub-folder next to this file),
so the plugin is fully self-contained: dropping the plugin folder into
"~/.local/share/zim/plugins/" is enough, there is no separate
installation step for the icon files.
'''

import os
import re
import logging

from gi.repository import GdkPixbuf

logger = logging.getLogger('zim.plugins.iconpages')


# Folder with the icon files bundled with this plugin.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(PLUGIN_DIR, 'icons')

# Default pixel size used to render icons (source files are scaled to fit).
DEFAULT_ICON_SIZE = 16

#: Icon file extensions, in order of preference when the same icon name
#: exists in more than one format.
#:
#: SVG comes first: it is rendered *at* the requested size rather than
#: scaled up from a fixed bitmap, which matters because the icon size
#: preference goes up to 64px, well past the 16px of the bundled PNGs.
#:
#: PNG before ICO: an .ico is just a container of fixed-size bitmaps, so
#: it has nothing to offer over a plain PNG here - GdkPixbuf picks one of
#: the contained images and scales it exactly like it would a PNG - while
#: the older variants of the format (palette colour, AND-mask
#: transparency) are the ones most likely to render badly. It is
#: supported because Windows icon sets ship in it, not because it is a
#: good source format.
ICON_EXTENSIONS = ('.svg', '.png', '.ico')

_supported_extensions = None  # cached result of _get_supported_extensions()


def _get_supported_extensions():
	'''Returns the subset of L{ICON_EXTENSIONS} that this GdkPixbuf
	install can actually decode.

	Not every format is built in: SVG comes from a separate loader module
	(librsvg), present on most Linux desktops and in the usual Windows
	GTK bundle, but not always - Zim's own core code says as much (see
	C{zim/gui/__init__.py}, where only png is scanned "not all installs
	have svg support"). ICO is normally built in, but there is no reason
	to take that on faith either. Without this check an icon in a format
	the local install cannot read would silently turn into the red "no
	image" placeholder with no hint as to why, so detect it once and say
	so in the log instead.

	Falls back to PNG only when GdkPixbuf cannot be queried at all: PNG
	is the one format Zim itself already relies on being present, and
	claiming support that is not there is what produces the silent
	placeholders this check exists to prevent.
	'''
	global _supported_extensions
	if _supported_extensions is None:
		try:
			available = set()
			for fmt in GdkPixbuf.Pixbuf.get_formats():
				available.update('.' + e.lower() for e in fmt.get_extensions())
			_supported_extensions = frozenset(
				ext for ext in ICON_EXTENSIONS if ext in available)
		except Exception:
			logger.exception('IconPages: could not query GdkPixbuf formats')
			_supported_extensions = frozenset(('.png',))
	return _supported_extensions

# Special / reserved icon names.
NO_IMAGE = '_no_image'                  # icon has no image
SEVERAL_ICONS = '_several_icons'        # ambiguous: several different icons apply
FOLDER_ICON = '_default_folder'
FILE_ICON = '_default_file'

# Names that are used internally and should not show up as "real" icons
# the user can pick from (e.g. in the "Insert Icon" dialog).
RESERVED_ICON_NAMES = frozenset((
	NO_IMAGE, SEVERAL_ICONS,
	FOLDER_ICON, FILE_ICON,
))

# Plain-color fallbacks for reserved names that need *some* pixbuf even
# when no matching png was bundled - see IconStore.get_pixbuf().
_PLACEHOLDER_COLORS = {
	NO_IMAGE: 0xcc4444ff,       # a page references an icon name that does not exist - reddish
	SEVERAL_ICONS: 0x6c6cccff,  # a page has more than one "[ICON=...]" shortcode - blueish
}


def _generate_placeholder(size, rgba):
	pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)
	pixbuf.fill(rgba)
	return pixbuf

# Icons are written in the page as bold text, e.g. "**[ICON=calendar]**".
# Bold ("strong") markup is used so the shortcode can be typed anywhere in
# a page without disturbing the normal text layout, and so the indexer can
# find it cheaply by only looking inside STRONG tokens.
PREFIX, POSTFIX = '[ICON=', ']'
# The name must be non-empty ("+", not "*"): "[ICON=]" is a typo, not a
# request for an icon called "". Matching it would store a row with an
# empty icon name in the index for no benefit.
ICON_RE = re.compile(re.escape(PREFIX) + r'([^\s\]]+)' + re.escape(POSTFIX))


def get_icon_markup(icon_name):
	'''Return the wiki text shortcode used to store C{icon_name} in a page,
	e.g. "**[ICON=calendar]**".
	'''
	return '**{}{}{}**'.format(PREFIX, icon_name, POSTFIX)


class IconStore(object):
	'''Loads icon files from L{ICONS_DIR} and caches them as
	C{GdkPixbuf.Pixbuf} objects for re-use in tree views, menus and
	dialogs.
	'''

	def __init__(self, icons_dir=ICONS_DIR):
		self.icons_dir = icons_dir
		self._names = None       # lowercase key -> on-disk display name (original case)
		self._files = None       # lowercase key -> actual file name, with extension
		self._pixbuf_cache = {}  # (lowercase name, size) -> Pixbuf or None

	def _scan(self):
		names = {}
		files = {}
		supported = _get_supported_extensions()
		skipped = []
		try:
			entries = sorted(os.listdir(self.icons_dir))
		except OSError:
			logger.warning('IconPages: could not read icon folder: %s', self.icons_dir)
			entries = []

		for fname in entries:
			if fname.startswith('.'):
				continue  # dotfiles, and macOS "._name" resource forks
			base, ext = os.path.splitext(fname)
			ext = ext.lower()
			if ext not in ICON_EXTENSIONS or not base:
				continue
			if ext not in supported:
				skipped.append(fname)
				continue

			key = base.lower()
			if key in files:
				# Same icon name in two formats - keep whichever comes
				# first in ICON_EXTENSIONS and note it, so a file that
				# looks like it should be used but is not does not turn
				# into a silent mystery.
				kept_ext = os.path.splitext(files[key])[1].lower()
				if ICON_EXTENSIONS.index(ext) >= ICON_EXTENSIONS.index(kept_ext):
					logger.info('IconPages: ignoring %s, %s takes precedence', fname, files[key])
					continue
				logger.info('IconPages: %s takes precedence over %s', fname, files[key])
			names[key] = base
			files[key] = fname

		if skipped:
			logger.warning(
				'IconPages: %i icon file(s) ignored - this GdkPixbuf install cannot'
				' decode them (supported here: %s; SVG needs the librsvg loader).'
				' Use PNG files instead. Ignored: %s',
				len(skipped), ', '.join(sorted(supported)), ', '.join(skipped))

		self._names = names
		self._files = files

	def list_names(self):
		'''Returns a sorted list of the icon names available for the user
		to pick (reserved / internal names are excluded), using each
		file's original capitalization.
		'''
		if self._names is None:
			self._scan()
		return sorted(
			display for key, display in self._names.items()
			if key not in RESERVED_ICON_NAMES
		)

	def has_icon(self, name):
		'''Case-insensitive check for whether an icon file exists for
		C{name} (e.g. a shortcode "[ICON=Attachment]" matches a file
		named "attachment.png", "Attachment.svg", "attachment.ico" or
		"ATTACHMENT.PNG").
		'''
		if self._names is None:
			self._scan()
		return bool(name) and name.lower() in self._names

	def get_pixbuf(self, name, size=DEFAULT_ICON_SIZE):
		'''Returns a C{GdkPixbuf.Pixbuf} for C{name} (matched
		case-insensitively against the bundled files - see
		L{has_icon()}), scaled to fit a C{size}xC{size} box. SVG sources
		are rendered directly at C{size} rather than scaled, so they stay
		sharp at any icon size.

		Falls back to a small generated placeholder for the reserved
		L{NO_IMAGE} / L{SEVERAL_ICONS} names when no matching file was
		bundled (a plain color square - drop a "_no_image.png" /
		"_several_icons.svg" file into the icons folder to replace it
		with real artwork). For any other, non-reserved name, returns
		C{None} when there is no matching file.
		'''
		if not name:
			return None

		key = (name.lower(), size)
		if key in self._pixbuf_cache:
			return self._pixbuf_cache[key]

		pixbuf = None
		if self.has_icon(name):
			path = os.path.join(self.icons_dir, self._files[name.lower()])
			try:
				pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
			except Exception:
				logger.exception('IconPages: failed to load icon: %s', path)
				pixbuf = None

		if pixbuf is None and name.lower() in _PLACEHOLDER_COLORS:
			pixbuf = _generate_placeholder(size, _PLACEHOLDER_COLORS[name.lower()])

		self._pixbuf_cache[key] = pixbuf
		return pixbuf

	def clear_cache(self):
		self._pixbuf_cache = {}
		self._names = None
		self._files = None


#: Module wide singleton - icons are a static resource shared by all
#: notebooks / windows, so a single cache is all that is needed.
ICONS = IconStore()
