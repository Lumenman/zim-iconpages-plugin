# -*- coding: utf-8 -*-

# Released under the GNU GPL version 3.
# This is a plugin for the Zim desktop wiki (zim-wiki.org).

'''Side pane widget showing the page tree with a small icon per page.

The icon for a page is picked in this order:
  1. an icon explicitly set on the page via a "[ICON=name]" shortcode
     (see L{iconutils}), tracked by the content indexer (L{indexer});
  2. otherwise a generic folder/file icon (depending on whether the
     page has sub-pages). These two are plain files in the "icons"
     folder - to restyle them, replace "_default_folder.png" /
     "_default_file.png".
'''

import logging
from collections import OrderedDict

from gi.repository import GObject
from gi.repository import GLib
from gi.repository import Gtk
from gi.repository import GdkPixbuf

from zim.notebook import Path
from zim.notebook.index.pages import PageIndexRecord, IndexNotFoundError
from zim.formats import get_parser

from zim.plugins.pageindex import (
	PageTreeStoreBase, PageTreeView,
	NAME_COL, TIP_COL,
)
from zim.notebook.index.pages import PagesTreeModelMixin
from zim.gui.widgets import (
	Dialog, WindowSidePaneWidget, ScrolledWindow, encode_markup_text,
)

from .iconutils import ICONS, get_icon_markup, \
	NO_IMAGE, FOLDER_ICON, FILE_ICON
from .indexer import IconsView

logger = logging.getLogger('zim.plugins.iconpages')


# Вычисляем индекс динамически, чтобы избежать TypeError, если Zim изменит базовые колонки
ICON_COL = len(PageTreeStoreBase.COLUMN_TYPES)


class IconsTreeStore(PagesTreeModelMixin, PageTreeStoreBase):
	'''C{PageTreeStore}-like model that adds an C{ICON_COL} with a
	C{GdkPixbuf.Pixbuf} for each page.

	Icon and name/tooltip lookups need extra database queries, so results
	are cached per page name. The cache uses an OrderedDict as an LRU cache 
	to bound memory use on large notebooks.
	'''

	COLUMN_TYPES = PageTreeStoreBase.COLUMN_TYPES + (GObject.TYPE_PYOBJECT,)  # ICON_COL

	CACHE_MAX_SIZE = 500  # Максимальное количество страниц в кэше

	def __init__(self, index, iconsview, icon_size):
		'''
		@param index: the notebook L{Index}
		@param iconsview: an L{IconsView} object, or C{None} if the icon
		table is not available yet (only the default folder/file icons
		are shown in that case)
		@param icon_size: pixel size to render icons at
		'''
		PagesTreeModelMixin.__init__(self, index)
		PageTreeStoreBase.__init__(self)

		self.iconsview = iconsview
		self.icon_size = icon_size

		self._cache = OrderedDict()

	def on_get_value(self, iter, column):
		if column in (NAME_COL, TIP_COL, ICON_COL):
			name = iter.row['name']
			row = self._cache.get(name)
			
			if row is None:
				row = self._compute_row(iter)
			else:
				# LRU: Перемещаем запрошенный элемент в конец (как недавно использованный)
				self._cache.move_to_end(name)
				
			return row[column]
		else:
			return PageTreeStoreBase.on_get_value(self, iter, column)

	def _compute_row(self, iter):
		page = PageIndexRecord(iter.row)
		name = page.basename

		row = {
			NAME_COL: name,
			TIP_COL: encode_markup_text(name),
			ICON_COL: self._get_pixbuf(page),
		}
		
		self._cache[page.name] = row
		
		# LRU: Если кэш превышает максимальный размер, удаляем самый старый элемент
		if len(self._cache) > self.CACHE_MAX_SIZE:
			self._cache.popitem(last=False)
			
		return row

	def _get_pixbuf(self, page):
		size = self.icon_size
		icon_name = self.iconsview.get_icon(page.name) if self.iconsview else None

		if icon_name:
			pixbuf = ICONS.get_pixbuf(icon_name, size)
		elif page.haschildren:
			pixbuf = ICONS.get_pixbuf(FOLDER_ICON, size)
		else:
			pixbuf = ICONS.get_pixbuf(FILE_ICON, size)

		# Guard against a stale/typo'd icon name that no longer matches
		# a bundled file: fall back to the "no image" placeholder rather
		# than storing None in the model.
		return pixbuf if pixbuf is not None else ICONS.get_pixbuf(NO_IMAGE, size)

	def teardown(self):
		'''Called via C{PageTreeView.disconnect_index()} when the model
		is dropped. 
		'''
		self._cache.clear()
		PagesTreeModelMixin.teardown(self)

	def invalidate_icons(self):
		'''Drop every cached row, so the next redraw recomputes each
		pixbuf from L{ICONS}.

		Used after the icon *files* on disk changed - the page -> icon
		name mapping in the index is still correct in that case, so
		nothing emits C{iconlist-changed} and no row would ever be
		recomputed on its own.
		'''
		self._cache.clear()

	def update_page(self, pagename):
		'''Drop C{pagename} from the cache and refresh it in the view.'''
		self._cache.pop(pagename, None)
		try:
			treepaths = self.find_all(Path(pagename))
		except IndexNotFoundError:
			return
		for treepath in treepaths:
			treeiter = self.get_iter(treepath)
			self.emit('row-changed', treepath, treeiter)

	def on_page_row_changed(self, o, row, oldrow):
		# The default folder/file icon depends on row['n_children'], and
		# the core emits this signal for the parent page whenever a child
		# page is created or deleted. Drop the cached row first, so the
		# redraw triggered by the mixin recomputes the pixbuf instead of
		# serving the stale one.
		self._cache.pop(row['name'], None)
		PagesTreeModelMixin.on_page_row_changed(self, o, row, oldrow)

	def on_page_row_deleted(self, o, row):
		# The cache is keyed by page name, which survives delete+recreate
		# of a page - without this a recreated page shows the icon of its
		# previous life until something else happens to refresh it.
		self._cache.pop(row['name'], None)
		PagesTreeModelMixin.on_page_row_deleted(self, o, row)


class IconsPageTreeView(PageTreeView):
	'''C{PageTreeView} with an extra left-hand column showing the page
	icon.
	'''

	def __init__(self, notebook, navigation):
		PageTreeView.__init__(self, notebook, navigation)

		# Put the icon renderer *inside* the existing "_pages_" column
		# (the one with the expander and the name text), positioned
		# before the text. GTK only applies depth-based indentation to
		# whichever column holds the expander - a separate icon column
		# would not indent along with the name text.
		pages_column = self.get_column(0)
		cr_icon = Gtk.CellRendererPixbuf()
		pages_column.pack_start(cr_icon, False)
		pages_column.reorder(cr_icon, 0)
		pages_column.set_attributes(cr_icon, pixbuf=ICON_COL)

	# --- diagnostics -------------------------------------------------
	#
	# The autoexpand/autocollapse machinery is entirely inherited from
	# PageTreeView and deliberately not overridden (see MAINTENANCE.md,
	# "ничего не добавлять поверх set_current_page()"). The two overrides
	# below add *nothing* but a log line each: they exist because "the
	# branch sometimes stays expanded" is impossible to tell apart from
	# the three cases where the core skips collapsing on purpose. Both
	# call straight through to the parent, and both are no-ops unless
	# zim is started with "--debug".

	def set_current_page(self, path, vivificate=False):
		if logger.isEnabledFor(logging.DEBUG):
			logger.debug(
				'IconPages: set_current_page(%s) selected=%s autoexpand=%s autocollapse=%s',
				path, self.get_selected_path(), self._autoexpand, self._autocollapse)
		treepath = PageTreeView.set_current_page(self, path, vivificate)
		if treepath is None:
			logger.debug(
				'IconPages: no row for %s - nothing selected, nothing collapsed', path)
		return treepath

	def restore_autoexpanded_path(self):
		if logger.isEnabledFor(logging.DEBUG):
			self._log_autocollapse_decision()
		PageTreeView.restore_autoexpanded_path(self)

	def _log_autocollapse_decision(self):
		'''Report which branch of C{restore_autoexpanded_path()} is about
		to be taken. Wrapped in its own try/except: a broken log line must
		never be able to take the side pane down.
		'''
		try:
			if not self._autoexpanded:
				logger.debug('IconPages: no collapse - nothing was auto-expanded')
				return
			ref1, ref2 = self._autoexpanded
			if not ref1.valid() or (ref2 is not None and not ref2.valid()):
				logger.debug('IconPages: no collapse - stale TreeRowReference')
				return
			prev = ref1.get_path()
			if self.get_any_children_expanded(prev):
				logger.debug(
					'IconPages: no collapse - a child of %s is expanded', prev)
			else:
				logger.debug(
					'IconPages: collapsing %s back to %s',
					prev, ref2.get_path() if ref2 is not None else 'top level')
		except Exception:
			logger.exception('IconPages: could not log autocollapse decision')


class IconPagesPluginWidget(Gtk.VBox, WindowSidePaneWidget):
	'''Side pane widget combining L{IconsPageTreeView} with the glue to
	reload the model when the plugin state changes.
	'''

	title = _('_IconPages')  # T: tab label for side pane

	def __init__(self, plugin, notebook, navigation, uistate):
		GObject.GObject.__init__(self)
		self.plugin = plugin
		self.notebook = notebook
		self.index = notebook.index
		self.navigation = navigation
		self.uistate = uistate

		# NOTE: uistate is a zim ConfigDict. Its __setitem__ raises
		# KeyError for a key that has no definition yet, and setdefault()
		# is what creates that definition - so always setdefault() before
		# assigning. An exception raised here takes down the whole side
		# pane, silently: the pane simply never appears.

		self.treeview = IconsPageTreeView(notebook, navigation)
		self.pack_start(ScrolledWindow(self.treeview), True, True, 0)

		#: Page currently open in the editor, re-applied after a model
		#: rebuild - see reload_model().
		self._current_page = None

		#: Icon size the current model was built with, to tell a real
		#: change apart from a spurious preferences 'changed' emission -
		#: see on_preferences_changed().
		self._applied_icon_size = None

		self.connectto(self.plugin.preferences, 'changed', self.on_preferences_changed)

		self.apply_view_preferences()
		self.reload_model()

	def set_page(self, page):
		'''Highlight C{page} in the tree, matching the built-in Page
		Index pane ("Содержание") exactly.

		C{PageTreeView.set_current_page()} already does the whole job:
		it collapses the branch that was auto-expanded for the previous
		page (C{restore_autoexpanded_path()}) and then expands and
		selects the new one. Nothing may be added on top of it here.
		'''
		self._current_page = page
		self.treeview.set_current_page(page, vivificate=True)

	def _get_iconsview(self):
		try:
			return IconsView.new_from_index(self.index)
		except ValueError:
			return None  # icon table not available (yet)

	def apply_view_preferences(self):
		'''Apply the view-level preferences to the treeview.

		Mirrors C{PageIndexNotebookViewExtension.on_preferences_changed()}
		- same options, same defaults - so this pane can serve as a full
		alternative to the built-in one. The two trees expand and collapse
		identically only while the matching options are set the same in
		both plugins.
		'''
		prefs = self.plugin.preferences
		self.treeview.set_use_drag_and_drop(prefs['use_drag_and_drop'])
		self.treeview.set_use_tooltip(prefs['use_tooltip'])
		self.treeview.set_use_ellipsize(not prefs['use_hscroll'])
		self.treeview.set_autoexpand(prefs['autoexpand'], prefs['autocollapse'])
		self.treeview.set_enable_tree_lines(prefs['tree_lines'])

	def on_preferences_changed(self, o):
		'''Re-apply the view options; rebuild the model only when the
		icon size preference really changed.
		'''
		self.apply_view_preferences()
		if self.plugin.preferences['icon_size'] != self._applied_icon_size:
			self.reload_model()

	def reload_model(self):
		'''Re-build the tree model. Needed whenever something changes
		that the model's per-page cache depends on (the icon size
		preference).
		'''
		old_model = self.treeview.get_model()
		expanded = []
		if old_model is not None:
			def remember(treeview, treepath, *a):
				treeiter = old_model.get_iter(treepath)
				expanded.append(old_model.get_indexpath(treeiter).name)
			self.treeview.map_expanded_rows(remember)

		self.treeview.disconnect_index()  # drop the old model's index signal connections
		iconsview = self._get_iconsview()
		self._applied_icon_size = self.plugin.preferences['icon_size']
		model = IconsTreeStore(self.index, iconsview, self._applied_icon_size)
		self.treeview.set_model(model)

		self.treeview._autoexpanded = None  # XXX no public API to reset this

		# Re-expand before set_page(): _store_expanded_path() records the
		# row to collapse back to on the next switch, and it must see the
		# restored expansion state to record the same row the built-in
		# pane has.
		for name in expanded:
			try:
				treepaths = model.find_all(Path(name))
			except IndexNotFoundError:
				continue
			for treepath in treepaths:
				self.treeview.expand_row(treepath, False)

		if self._current_page is not None:
			self.set_page(self._current_page)

	def update_page(self, pagename):
		'''Refresh a single page (e.g. after its icon shortcode changed)
		without rebuilding the whole model.
		'''
		model = self.treeview.get_model()
		if isinstance(model, IconsTreeStore):
			model.update_page(pagename)

	def refresh_icons(self):
		'''Re-read the icon files from disk and redraw the tree.

		The other half of the "Rescan Icons" action: L{rescan()} on the
		notebook extension refreshes the page -> icon *name* mapping,
		this refreshes the *images* those names resolve to. Without it,
		adding or replacing a file in the "icons" folder has no visible
		effect until Zim is restarted - the file listing and the
		rendered pixbufs are both cached in the process-wide L{ICONS}
		store, and a page whose shortcode did not change never emits
		C{iconlist-changed} to trigger a redraw either.

		Deliberately does not call reload_model(): dropping the caches
		and asking for a redraw is enough, and rebuilding the model
		would collapse the tree (see MAINTENANCE.md).
		'''
		ICONS.clear_cache()
		model = self.treeview.get_model()
		if isinstance(model, IconsTreeStore):
			model.invalidate_icons()
		# GtkTreeView pulls every cell value from the model again while
		# drawing, so an empty row cache plus a redraw is all it takes.
		self.treeview.queue_draw()

	def insert_icon(self, pageview):
		'''Run the "Insert Icon" dialog for C{pageview}.'''
		InsertIconDialog(pageview).run()


class InsertIconDialog(Dialog):
	'''Small dialog with an icon grid; picking an icon inserts its
	"[ICON=name]" shortcode at the cursor position in C{pageview}.
	'''

	def __init__(self, pageview):
		Dialog.__init__(
			self, pageview, _('Insert Icon'),  # T: dialog title
			buttons=Gtk.ButtonsType.OK_CANCEL,
			defaultwindowsize=(320, 380),
		)
		self.pageview = pageview
		self._chosen = None

		self.model = Gtk.ListStore(GdkPixbuf.Pixbuf, str)  # icon, name
		for name in ICONS.list_names():
			pixbuf = ICONS.get_pixbuf(name, 32)
			if pixbuf is not None:
				self.model.append([pixbuf, name])

		self.iconview = Gtk.IconView(self.model)
		self.iconview.set_pixbuf_column(0)
		self.iconview.set_text_column(1)
		self.iconview.set_item_width(64)
		self.iconview.set_columns(-1)  # auto-compute based on available width
		self.iconview.set_property('activate-on-single-click', True)
		self.iconview.connect('item-activated', self.on_activated)
		self.iconview.connect('selection-changed', self.on_selection_changed)

		scrolled = Gtk.ScrolledWindow()
		scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
		scrolled.set_shadow_type(Gtk.ShadowType.IN)
		scrolled.add(self.iconview)
		self.vbox.pack_start(scrolled, True, True, 0)

	def on_selection_changed(self, iconview):
		items = iconview.get_selected_items()
		self._chosen = self.model[items[0]][1] if items else None

	def on_activated(self, iconview, treepath):
		self._chosen = self.model[treepath][1]
		self.response(Gtk.ResponseType.OK)

	def do_response_ok(self):
		if not self._chosen:
			return False
		tree = get_parser('wiki').parse(get_icon_markup(self._chosen))
		buffer = self.pageview.textview.get_buffer()
		buffer.insert_parsetree_at_cursor(tree, interactive=True)
		return True
