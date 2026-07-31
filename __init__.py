# -*- coding: utf-8 -*-

# Released under the GNU GPL version 3.
# This is a plugin for the Zim desktop wiki (zim-wiki.org).

'''IconPages plugin for Zim.

Adds a side pane with the page tree, showing a small icon per page. The
icon is either set explicitly on a page (via a "[ICON=name]" shortcode,
see L{iconutils} and L{indexer}), or a generic folder/file icon.

The icon files themselves ship inside this plugin's own folder (see the
"icons" sub-folder next to this file) - no separate installation step is
needed, and the two defaults can be restyled by simply replacing
"_default_folder.png" / "_default_file.png".
'''

import logging

from gi.repository import GLib

from zim.plugins import PluginClass, find_extension
from zim.actions import action
from zim.notebook import NotebookExtension, Path
from zim.gui.widgets import LEFT_PANE, PANE_POSITIONS

from zim.gui.notebookview import NotebookViewExtension

from .indexer import IconsIndexer
from .panelview import IconPagesPluginWidget

logger = logging.getLogger('zim.plugins.iconpages')


class IconPagesPlugin(PluginClass):

	plugin_info = {
		'name': _('Icon Pages'),  # T: plugin name
		'description': _('''\
This plugin shows the page index with a small icon for each page.
Icons can be set for individual pages with a "[ICON=name]" shortcode.
'''),  # T: plugin description
		'author': 'Lumenman',
		'help': 'Plugins:IconPages',
	}

	plugin_preferences = (
		# key, type, label, default
		('pane', 'choice', _('Position in the window'), LEFT_PANE, PANE_POSITIONS),
			# T: option for plugin preferences
		('icon_size', 'int', _('Icon size (pixels)'), 16, (8, 64)),
			# T: option for plugin preferences
		# The same view options the built-in Page Index pane has, with the
		# same labels and defaults (so existing translations apply). This
		# makes the pane a full alternative to Page Index; the two trees
		# behave identically only while the matching options are set the
		# same in both plugins.
		('use_drag_and_drop', 'bool', _('Use drag&drop to move pages in the notebook'), False),
			# T: preferences option
		('autoexpand', 'bool', _('Automatically expand sections on open page'), True),
			# T: preferences option
		('autocollapse', 'bool', _('Automatically collapse sections on close page'), True),
			# T: preferences option
		('use_hscroll', 'bool', _('Use horizontal scrollbar (may need restart)'), False),
			# T: preferences option
		('use_tooltip', 'bool', _('Use tooltips'), True),
			# T: preferences option
		('tree_lines', 'bool', _('Show tree lines'), False),
			# T: preferences option
	)


class IconPagesNotebookExtension(NotebookExtension):
	'''Hooks the L{IconsIndexer} into the notebook index. Kept separate
	from the per-window side pane extension since the index is shared
	between all windows on the same notebook.

	Scanning page content is what makes the "[ICON=name]" shortcode work
	at all, and there is no other way to give a page its own icon, so it
	is not optional - it is simply what the plugin does. Disabling the
	plugin removes the table again (see L{teardown()}).
	'''

	__signals__ = {
		'iconlist-changed': (None, None, (object,)),
	}

	BACKFILL_CHUNK = 20  # pages parsed per idle callback

	def __init__(self, plugin, notebook):
		NotebookExtension.__init__(self, plugin, notebook)
		self.index = notebook.index
		self.indexer = None

		self._backfill_source = None  # GLib source id of the running scan
		self._backfill_rows = []
		self._backfill_pos = 0

		self.connectto(self.index, 'new-update-iter', self._on_new_update_iter)
		self._enable_indexer()

	def _on_new_update_iter(self, index, update_iter):
		# A fresh update_iter means a full reindex from scratch - the old
		# indexer object (tied to the previous update_iter) is gone, so
		# re-create it.
		self.indexer = None
		self._enable_indexer()

	def _enable_indexer(self):
		if self.indexer is not None:
			return

		if self.index.get_property(IconsIndexer.PLUGIN_NAME) != IconsIndexer.PLUGIN_DB_FORMAT:
			IconsIndexer.teardown_db(self.index._db)
			rebuild = True
		else:
			rebuild = False

		self.indexer = IconsIndexer.new_from_index(self.index)
		self.index.update_iter.add_indexer(self.indexer)
		self.connectto(self.indexer, 'iconlist-changed', self.on_iconlist_changed)

		if rebuild:
			self.rescan()

	def rescan(self):
		'''Re-read every page from disk and refresh the icon index.

		This is the plugin's self-heal path, exposed to the user as the
		"Rescan Icons" action: the plugin's own index can end up holding
		a stale icon for a page if a save was interrupted (e.g. a
		Dropbox file lock on Windows), and Zim's own background
		reindexing will not notice, because it compares file mtimes
		rather than content.

		Deliberately does *not* drop the table first, for two reasons:
		the scan is spread over many idle callbacks, so an emptied table
		would make every icon disappear and then trickle back in over
		several seconds; and simply re-reading a page already produces
		the correct result (a changed shortcode overwrites the row, a
		removed one deletes it). Rows for pages that no longer exist at
		all are swept up at the end of the scan.
		'''
		self._backfill_existing_pages()

	def _backfill_existing_pages(self):
		'''Scan pages that are already in the index for "[ICON=name]"
		shortcodes, without using C{Index.flag_reindex()}.

		C{flag_reindex()} marks every page's *source file* for
		re-indexing, but its underlying SQL also (incorrectly) catches
		the notebook's root folder, because the root page's own
		C{source_file} happens to point at the root folder's row. That
		makes Zim's core file indexer briefly treat all top-level pages
		as "missing" and re-insert them (see C{update_folder()} in
		C{zim/notebook/index/files.py}) - harmless on its own, but any
		data an indexer (like this one) already wrote for those pages
		in between gets lost, and it is needless churn on the core
		index for what should be a read-only scan. Reading each page's
		current content directly here avoids touching the file index
		at all.

		The scan parses every page in the notebook, which is far too
		slow to do in one go on the main loop - on a large notebook that
		would freeze the whole UI for as long as it takes. So it runs in
		small chunks from an idle handler instead; icons simply refresh
		in place as the scan progresses.

		Crucially it never calls C{reload_model()}: that would build a
		new C{Gtk.TreeModel}, which collapses the whole tree. Updates
		reach the side pane one page at a time through the
		C{iconlist-changed} signal, which only redraws that page's row,
		so expanded branches and scroll position survive a rescan.
		'''
		self._cancel_backfill()
		rows = self.index._db.execute(  # XXX private API
			'SELECT * FROM pages WHERE source_file IS NOT NULL'
		).fetchall()
		if not rows:
			return
		logger.info('IconPages: scanning %i pages for icon shortcodes', len(rows))
		self._backfill_rows = rows
		self._backfill_pos = 0
		self._backfill_source = GLib.idle_add(self._backfill_step)

	def _backfill_step(self):
		'''Process one chunk of L{_backfill_existing_pages()}.
		@returns: C{True} while there is more work to do.
		'''
		# NOTE: when this returns False, GLib removes the source itself,
		# so the state is reset *without* calling GLib.source_remove() -
		# doing both would remove the same source id twice and log a
		# "Source ID was not found" critical. Only _cancel_backfill(),
		# called from outside the callback, removes the source.
		if self.indexer is None:
			self._reset_backfill_state()
			return False

		chunk = self._backfill_rows[self._backfill_pos:self._backfill_pos + self.BACKFILL_CHUNK]
		self._backfill_pos += len(chunk)

		for row in chunk:
			try:
				page = self.notebook.get_page(Path(row['name']))
				tree = page.get_parsetree()
			except Exception:
				logger.exception(
					'IconPages: failed to read page during icon scan: %s', row['name'])
				continue
			if tree is not None:
				self.indexer.on_page_changed(None, row, tree)

		if self._backfill_pos >= len(self._backfill_rows):
			dropped = self.indexer.drop_orphans()
			logger.info('IconPages: icon scan finished (%i orphan rows removed)', dropped)
			self._reset_backfill_state()
			return False
		return True

	def _cancel_backfill(self):
		'''Stop a running scan. Safe to call when none is running, but
		must not be called from inside L{_backfill_step()} - see the note
		there.
		'''
		if self._backfill_source is not None:
			GLib.source_remove(self._backfill_source)
		self._reset_backfill_state()

	def _reset_backfill_state(self):
		self._backfill_source = None
		self._backfill_rows = []
		self._backfill_pos = 0

	def on_iconlist_changed(self, indexer, pagename):
		# Re-emit at the extension level: this object lives for as long
		# as the notebook is open, while self.indexer may be replaced
		# (e.g. after a full reindex). Side pane widgets look this
		# extension up via find_extension() rather than holding a direct
		# reference to the indexer.
		self.emit('iconlist-changed', pagename)

	def teardown(self):
		'''Called when the user disables the plugin (not on application
		exit), so this is also where the plugin's own table is removed
		from the notebook index again.
		'''
		self._cancel_backfill()
		if self.indexer is None:
			return
		self.disconnect_from(self.indexer)
		self.indexer.disconnect_all()
		self.index.update_iter.remove_indexer(self.indexer)
		
		IconsIndexer.teardown_db(self.index._db)
		
		self.index.set_property(IconsIndexer.PLUGIN_NAME, None)
		self.indexer = None


class IconPagesNotebookViewExtension(NotebookViewExtension):
	'''Adds the side pane widget, and the "Insert Icon" / "Rescan Icons"
	actions, to each (navigable) main window.
	'''

	def __init__(self, plugin, pageview):
		NotebookViewExtension.__init__(self, plugin, pageview)

		self.widget = IconPagesPluginWidget(
			plugin, pageview.notebook, self.navigation, self.uistate)
		self.add_sidepane_widget(self.widget, 'pane')

		self.on_page_changed(pageview, pageview.page)
		self.connectto(pageview, 'page-changed')

		self.notebook_ext = find_extension(pageview.notebook, IconPagesNotebookExtension)
		if self.notebook_ext is not None:
			self.connectto(self.notebook_ext, 'iconlist-changed',
				lambda o, pagename: self.widget.update_page(pagename))
		else:
			# Should not happen, but an exception raised here would take
			# the whole side pane down (silently - the pane simply never
			# appears). Degrade instead: icons still render, they just
			# will not refresh live when a shortcode is edited.
			logger.warning(
				'IconPages: no IconPagesNotebookExtension found for this notebook'
				' - icons will not update live')

	def on_page_changed(self, pageview, page):
		self.widget.set_page(page)

	@action(_('Insert _Icon...'), menuhints='insert')  # T: menu item
	def insert_icon(self):
		'''Show the "Insert Icon" dialog and insert the chosen icon's
		shortcode at the cursor position.
		'''
		self.widget.insert_icon(self.pageview)

	@action(_('_Rescan Icons'), menuhints='tools')  # T: menu item
	def rescan_icons(self):
		'''Refresh the icons shown in the side pane.

		Two independent things can be stale, and the action has to cover
		both - covering only the second one is what made it look like it
		did nothing at all after the icon files were replaced:

		  1. the icon *files*: the file listing and the rendered pixbufs
		     are cached process-wide in L{ICONS}, so a file added to or
		     replaced in the "icons" folder is not picked up until the
		     caches are dropped;
		  2. the page -> icon *name* mapping in the index, which the
		     notebook extension refreshes by re-reading every page.

		Neither path rebuilds the tree model, so expanded branches and
		scroll position survive (see MAINTENANCE.md).
		'''
		self.widget.refresh_icons()

		if self.notebook_ext is None:
			logger.warning('IconPages: cannot rescan - no notebook extension')
			return
		self.notebook_ext.rescan()

	def teardown(self):
		self.widget.treeview.disconnect_index()
		NotebookViewExtension.teardown(self)
