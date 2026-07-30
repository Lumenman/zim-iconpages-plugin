'''Regression test for the IconPages plugin: live editing (typing a
shortcode into an already-open page), page deletion cleanup, and
that a full page-content backfill does not disturb the tree's
expand/collapse state.

See test_basic.py / ../MAINTENANCE.md for how to run this.
'''

import sys
import traceback

import tests
import zim.config
import zim.plugins
from zim.plugins import PluginManager, find_extension
from zim.notebook import Path
from zim.plugins.pageindex import PageTreeStoreBase

ICON_COL = len(PageTreeStoreBase.COLUMN_TYPES)

zim.config.manager.makeConfigManagerVirtual()
zim.plugins.resetPluginManager()

from tests.mainwindow import setUpMainWindow


def step(name):
	print('--- %s' % name)


class T(tests.TestCase):
	def runTest(self):
		pass


t = T()
t.setUpClass()

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

try:
	plugin = PluginManager.load_plugin('iconpages')

	content = {
		'Alpha': 'No icon yet.\n',
		'Beta': 'Some text\n',
	}
	notebook = t.setUpNotebook(content=content)
	mainwindow = setUpMainWindow(notebook, path='Alpha')
	ext = find_extension(mainwindow.pageview, iconpages.IconPagesNotebookViewExtension)
	notebook_ext = find_extension(notebook, iconpages.IconPagesNotebookExtension)

	from zim.plugins.iconpages.indexer import IconsView
	view = IconsView.new_from_index(notebook.index)

	step('live edit: type an icon shortcode into the open page and store it')
	page = notebook.get_page(Path('Alpha'))
	page.parse('wiki', 'Now with an icon.\n\n**[ICON=note]**\n')
	notebook.store_page(page)
	print('icon(Alpha) after edit =', view.get_icon('Alpha'))
	assert view.get_icon('Alpha') == 'note'

	step('widget.update_page gets called live via iconlist-changed -> uistate/model')
	model = ext.widget.treeview.get_model()
	def find_row_for_page(treeiter, target):
		while treeiter is not None:
			path = model.get_value(treeiter, 1)
			if path is not None and getattr(path, 'name', None) == target:
				return treeiter
			child = model.iter_children(treeiter)
			if child is not None:
				r = find_row_for_page(child, target)
				if r is not None:
					return r
			treeiter = model.iter_next(treeiter)
		return None
	treeiter = find_row_for_page(model.get_iter_first(), 'Alpha')
	icon1 = model.get_value(treeiter, ICON_COL)
	print('icon pixbuf for Alpha in tree (after live edit, no full reload called):', icon1)
	assert icon1 is not None

	step('remove the icon shortcode again')
	page.parse('wiki', 'Icon removed now.\n')
	notebook.store_page(page)
	print('icon(Alpha) after removing shortcode =', view.get_icon('Alpha'))
	assert view.get_icon('Alpha') is None

	step('delete a page entirely -> icon row removed from index')
	content2 = {'Gamma': '**[ICON=find]**\n'}
	notebook2 = t.setUpNotebook(name='testnotebook2', content=content2)
	view2 = IconsView.new_from_index(notebook2.index)
	print('icon(Gamma) before delete =', view2.get_icon('Gamma'))
	assert view2.get_icon('Gamma') == 'find'
	notebook2.delete_page(Path('Gamma'))
	print('icon(Gamma) after delete =', view2.get_icon('Gamma'))
	assert view2.get_icon('Gamma') is None
	print('OK - deleted page icon entry cleaned up')

	step('rescan does not collapse the tree')
	# The scan runs for real on a first-time index build, after a full
	# reindex-from-scratch, and whenever the user picks "Rescan Icons".
	# It must never disrupt the tree's expand state, since it only pushes
	# targeted per-page "iconlist-changed" updates (see update_page() in
	# panelview.py), not a whole-model reload.
	content4 = {'Zeta': 'z\n', 'Zeta:Eta': '**[ICON=home]**\n'}
	notebook4 = t.setUpNotebook(name='testnotebook4', content=content4)
	mainwindow4 = setUpMainWindow(notebook4, path='Zeta:Eta')
	ext4 = find_extension(mainwindow4.pageview, iconpages.IconPagesNotebookViewExtension)
	notebook_ext4 = find_extension(notebook4, iconpages.IconPagesNotebookExtension)

	from gi.repository import Gtk
	model_before = ext4.widget.treeview.get_model()
	parent_treepath = Gtk.TreePath.new_from_indices([0])  # 'Zeta' - should be expanded to reveal 'Eta'
	assert ext4.widget.treeview.row_expanded(parent_treepath), \
		'parent row should already be expanded to show the initially-open page'

	# Go through the user-facing action, not the private method.
	ext4.rescan_icons()
	drain_main_loop()  # the scan itself runs from an idle handler
	assert notebook_ext4._backfill_source is None, 'rescan did not finish'

	model_after = ext4.widget.treeview.get_model()
	assert model_after is model_before, \
		'rescan replaced the tree model - it should only push per-page updates'
	assert ext4.widget.treeview.row_expanded(parent_treepath), \
		'tree collapsed after rescan'
	print('OK - "Rescan Icons" keeps the same model and does not collapse the tree')

	step('default folder/file icon follows child page creation and deletion')
	# The default icon depends on whether the page has children, and the
	# per-page row cache in IconsTreeStore is keyed by page name. The core
	# emits page-row-changed for the parent when a child is created or
	# deleted; the cached row must be dropped then, or the tree keeps
	# showing the stale file icon after a first child appears (and the
	# stale folder icon after the last child is gone).
	from zim.plugins.iconpages.iconutils import ICONS, FOLDER_ICON, FILE_ICON

	model = ext.widget.treeview.get_model()
	size = ext.widget.plugin.preferences['icon_size']

	def pixbuf_for(pagename):
		treeiter = find_row_for_page(model.get_iter_first(), pagename)
		return model.get_value(treeiter, ICON_COL)

	# read the icon first, so the row is in the cache before the child appears
	assert pixbuf_for('Beta') is ICONS.get_pixbuf(FILE_ICON, size), \
		'leaf page should show the default file icon'

	child = notebook.get_page(Path('Beta:Child'))
	child.parse('wiki', 'child\n')
	notebook.store_page(child)
	drain_main_loop()
	assert pixbuf_for('Beta') is ICONS.get_pixbuf(FOLDER_ICON, size), \
		'page that got its first child must switch to the folder icon without a reload'

	notebook.delete_page(Path('Beta:Child'))
	drain_main_loop()
	assert pixbuf_for('Beta') is ICONS.get_pixbuf(FILE_ICON, size), \
		'page whose last child was deleted must switch back to the file icon'
	print('OK - folder/file icon follows child creation/deletion live')

	step('cleanup')
	PluginManager.remove_plugin('iconpages')
	print('OK')

	print('\nALL ROUND-2 CHECKS PASSED')

except Exception:
	traceback.print_exc()
	sys.exit(1)
