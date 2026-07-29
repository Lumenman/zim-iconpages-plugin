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


ICON_COL = 7  #: extra column with a GdkPixbuf.Pixbuf (or None)


class IconsTreeStore(PagesTreeModelMixin, PageTreeStoreBase):
	'''C{PageTreeStore}-like model that adds an C{ICON_COL} with a
	C{GdkPixbuf.Pixbuf} for each page.

	Icon and name/tooltip lookups need extra database queries, so results
	are cached per page name. The cache is invalidated for a single page
	via L{update_page()} (e.g. when its icon shortcode changes) and is
	dropped completely once it grows large, to bound memory use on large
	notebooks.
	'''

	COLUMN_TYPES = PageTreeStoreBase.COLUMN_TYPES + (GObject.TYPE_PYOBJECT,)  # ICON_COL

	CACHE_CLEAR_INTERVAL = 500  # milliseconds
	CACHE_MAX_SIZE = 500        # pages

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

		self._cache = {}
		self._clear_source = None  # GLib source id of the pending cache clear

	def on_get_value(self, iter, column):
		if column in (NAME_COL, TIP_COL, ICON_COL):
			row = self._cache.get(iter.row['name'])
			if row is None:
				row = self._compute_row(iter)
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
		self._schedule_cache_clear()
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

	def _schedule_cache_clear(self):
		if self._clear_source is not None:
			return

		def _clear():
			if len(self._cache) > self.CACHE_MAX_SIZE:
				self._cache = {}
			self._clear_source = None
			return False  # do not call again

		self._clear_source = GLib.timeout_add(self.CACHE_CLEAR_INTERVAL, _clear)

	def teardown(self):
		'''Called via C{PageTreeView.disconnect_index()} when the model
		is dropped. Cancel the pending cache-clear timeout: its closure
		keeps this model alive until it fires, and it would run against
		a model that is no longer in use.
		'''
		if self._clear_source is not None:
			GLib.source_remove(self._clear_source)
			self._clear_source = None
		self._cache = {}
		PagesTreeModelMixin.teardown(self)

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

		This used to re-check the selection afterwards and call
		C{select_treepath()} a second time when it did not match. That
		looked harmless but was the reason this tree collapsed branches
		differently from the built-in one: C{select_treepath()} also
		calls C{_store_expanded_path()}, which records *where to
		collapse back to* next time. Running it again after the branch
		has just been expanded stores the already-expanded row as the
		restore point, so the next page switch collapses less than it
		should - the branch stays open. It fired whenever
		C{get_selected_path()} did not return the page, e.g. for rows
		with no index record behind them (placeholders).
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

		All of these are cheap, idempotent treeview setters that do not
		touch the model - re-applying them must never grow into a
		C{reload_model()}, which is reserved for C{icon_size} (see
		L{on_preferences_changed()}): a model rebuild costs the tree its
		expanded branches on every spurious 'changed' emission.
		'''
		prefs = self.plugin.preferences
		self.treeview.set_use_drag_and_drop(prefs['use_drag_and_drop'])
		self.treeview.set_use_tooltip(prefs['use_tooltip'])
		self.treeview.set_use_ellipsize(not prefs['use_hscroll'])
			# to use horizontal scrolling, ellipsize must be off
		self.treeview.set_autoexpand(prefs['autoexpand'], prefs['autocollapse'])
		self.treeview.set_enable_tree_lines(prefs['tree_lines'])

	def on_preferences_changed(self, o):
		'''Re-apply the view options; rebuild the model only when the
		icon size preference really changed.

		The 'changed' signal fires far more often than an actual edit of
		this plugin's settings: a bare OK in the "Configure Plugin"
		dialog re-writes the same values, and - via the gio file monitor
		on C{preferences.conf} - so does *any* rewrite of that file
		(saving any preference from any dialog, a second Zim process, a
		sync/backup tool touching the file). Zim's C{ControlledDict}
		emits 'changed' even when the stored value is identical. Each
		rebuild collapses branches the user expanded by hand, so
		reacting to every emission made this tree drift away from the
		built-in Page Index pane at seemingly random moments. The view
		options are harmless to re-apply, so they take the simple path.
		'''
		self.apply_view_preferences()
		if self.plugin.preferences['icon_size'] != self._applied_icon_size:
			self.reload_model()

	def reload_model(self):
		'''Re-build the tree model. Needed whenever something changes
		that the model's per-page cache depends on (the icon size
		preference).

		Unlike the built-in Page Index pane, which builds its model once
		and keeps it for the lifetime of the window, this widget can swap
		the model at runtime. Three things then have to be put back by
		hand, or the tree quietly stops behaving like Page Index:

		 1. the fresh model has no "current page", so the tree no longer
		    follows the page open in the editor until the next switch;
		 2. C{PageTreeView._autoexpanded} holds C{Gtk.TreeRowReference}
		    objects created against the *old* model
		    (C{_store_expanded_path()}). They are not cleared by
		    C{set_model()}, and C{restore_autoexpanded_path()} would
		    happily read a path out of them and collapse whatever row
		    now sits at that position in the new tree;
		 3. C{set_model()} drops the expanded/collapsed state of every
		    row, while the built-in pane keeps its state - so whatever
		    was expanded (including branches the user opened by hand) is
		    recorded first and re-expanded on the new model.
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

		# Built manually rather than via zim's ScrolledWindow() helper:
		# that helper wraps anything that isn't a TextView/TreeView/Layout
		# in an extra Gtk.Viewport (add_with_viewport), which does not
		# know that Gtk.IconView is natively scrollable. That extra layer
		# broke the width-for-height negotiation needed for the icons to
		# wrap across the available width - they ended up in 2 wide rows
		# with no way to reach the rest once horizontal scrolling is
		# disabled. Adding the IconView directly avoids that.
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
		# Insert a real parsed tree (so the icon shortcode becomes actual
		# bold formatting right away) instead of inserting the literal
		# "**[ICON=name]**" characters as plain text: the latter depends
		# on a later save+reparse round-trip to turn those asterisks into
		# real formatting, which is fragile - e.g. when replacing an
		# already-set icon, or inserting right next to other formatted
		# text, it could produce a malformed or merged run.
		tree = get_parser('wiki').parse(get_icon_markup(self._chosen))
		buffer = self.pageview.textview.get_buffer()
		buffer.insert_parsetree_at_cursor(tree, interactive=True)
		return True
