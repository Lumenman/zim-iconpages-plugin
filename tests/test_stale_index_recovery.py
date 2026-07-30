'''Regression test for recovering from a stale/incorrect entry in the
plugin's own icon index (as could happen after an interrupted save, e.g.
a Dropbox file lock on Windows - see README.md).

The user-facing way to trigger the self-heal is the "Rescan Icons"
action. Besides fixing the entry, it must leave the tree alone: the scan
pushes per-page updates rather than rebuilding the model, so expanded
branches and scroll position survive. Both halves are checked here.
'''
import sys, traceback
import tests, zim.config, zim.plugins
from zim.plugins import PluginManager, find_extension
from zim.notebook import Path
from zim.plugins.pageindex import PageTreeStoreBase

ICON_COL = len(PageTreeStoreBase.COLUMN_TYPES)

zim.config.manager.makeConfigManagerVirtual()
zim.plugins.resetPluginManager()
from tests.mainwindow import setUpMainWindow

iconpages = __import__('zim.plugins.iconpages', fromlist=['x'])


def drain_main_loop():
	'''Run any pending GLib idle/timeout callbacks.

	The plugin's initial icon scan (_backfill_existing_pages) runs in
	chunks from an idle handler so it cannot freeze the UI on a large
	notebook. These tests never spin a main loop of their own, so they
	have to drain it explicitly to let that scan finish.
	'''
	from gi.repository import GLib
	ctx = GLib.MainContext.default()
	while ctx.pending():
		ctx.iteration(False)


class T(tests.TestCase):
	def runTest(self):
		pass

t = T()
t.setUpClass()

try:
	plugin = PluginManager.load_plugin('iconpages')

	content = {'Home': '**[ICON=attachment]**\n', 'Other': 'no icon\n'}
	notebook = t.setUpNotebook(content=content)
	notebook.index.check_and_update()

	mainwindow = setUpMainWindow(notebook, path='Home')
	ext = find_extension(mainwindow.pageview, iconpages.IconPagesNotebookViewExtension)

	from zim.plugins.iconpages.indexer import IconsView
	view = IconsView.new_from_index(notebook.index)
	assert view.get_icon('Home') == 'attachment'
	print('OK - initial state correct')

	# Simulate a stale/corrupted entry - the file on disk is untouched
	# and still correct, only the plugin's own index row is wrong.
	db = notebook.index._db
	db.execute("UPDATE iconlist SET icon = ? WHERE id = ?", ('some_garbage_name', 'Home'))
	db.commit()
	assert view.get_icon('Home') == 'some_garbage_name'
	print('OK - corruption simulated')

	# A normal check_and_update should NOT fix this (file did not change)
	notebook.index.check_and_update()
	assert view.get_icon('Home') == 'some_garbage_name'
	print('OK - confirmed normal reindex does not self-heal (as expected)')

	# The fix: the "Rescan Icons" action. Capture the model and the
	# expanded state first - the whole point is that neither changes.
	from gi.repository import Gtk
	model_before = ext.widget.treeview.get_model()
	ext.widget.treeview.expand_all()
	expanded_before = []
	ext.widget.treeview.map_expanded_rows(
		lambda tv, path, data=None: expanded_before.append(path.to_string()))
	assert expanded_before, 'nothing expanded - the test would prove nothing'

	ext.rescan_icons()
	drain_main_loop()  # the scan runs from an idle handler

	notebook_ext = find_extension(notebook, iconpages.IconPagesNotebookExtension)
	assert notebook_ext._backfill_source is None, 'rescan did not finish'
	assert view.get_icon('Home') == 'attachment', view.get_icon('Home')
	print('OK - "Rescan Icons" corrected the stale entry')

	model = ext.widget.treeview.get_model()
	assert model is model_before, \
		'rescan replaced the tree model - the tree would collapse'
	expanded_after = []
	ext.widget.treeview.map_expanded_rows(
		lambda tv, path, data=None: expanded_after.append(path.to_string()))
	assert expanded_after == expanded_before, \
		'rescan changed which rows are expanded: %r -> %r' % (expanded_before, expanded_after)
	print('OK - tree model and expanded rows survived the rescan')

	def find_row(treeiter, target):
		while treeiter is not None:
			path = model.get_value(treeiter, 1)
			if path is not None and getattr(path, 'name', None) == target:
				return treeiter
			child = model.iter_children(treeiter)
			if child is not None:
				r = find_row(child, target)
				if r is not None:
					return r
			treeiter = model.iter_next(treeiter)
		return None
	treeiter = find_row(model.get_iter_first(), 'Home')
	assert treeiter is not None
	icon = model.get_value(treeiter, ICON_COL)
	assert icon is not None
	print('OK - side panel model shows the corrected icon')

	# Orphan sweep: a row for a page that no longer exists is dropped at
	# the end of a rescan.
	db.execute("INSERT OR REPLACE INTO iconlist (id, icon) VALUES (?, ?)",
		('Ghost:Deleted:Page', 'home'))
	db.commit()
	assert view.get_icon('Ghost:Deleted:Page') == 'home'
	ext.rescan_icons()
	drain_main_loop()
	assert view.get_icon('Ghost:Deleted:Page') is None, 'orphan row was not swept'
	print('OK - orphan rows removed by rescan')

	print('\nALL STALE-INDEX-RECOVERY CHECKS PASSED')
except Exception:
	traceback.print_exc()
	sys.exit(1)
