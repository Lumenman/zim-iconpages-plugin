'''Regression test for the IconPages plugin: indexing, side panel model,
Insert Icon dialog, enable/disable + full teardown.

HOW TO RUN (see ../MAINTENANCE.md for the full setup):
    1. Copy this file into the root of a Zim source checkout (the folder
       that contains the "zim" and "tests" packages).
    2. Put (or symlink) this plugin's folder at zim/plugins/iconpages
       inside that checkout.
    3. From that checkout folder:  xvfb-run -a python3 test_basic.py
    Exits with status 0 and prints "ALL CHECKS PASSED" on success.
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
	step('load plugin')
	plugin = PluginManager.load_plugin('iconpages')
	assert plugin.__class__.__name__ == 'IconPagesPlugin'
	print('OK - plugin loaded:', plugin)

	step('create notebook with pages and icon shortcodes')
	content = {
		'Home': 'Content **[ICON=home]** here.\n',
		'Projects': 'Some text\n',
		'Projects:Alpha': 'Alpha project page.\n',
		'Projects:Beta': 'Beta project page.\n',
		'Notes': 'Random notes.\n',
		'Notes:Ambiguous': '**[ICON=calendar]** and **[ICON=mail]** in same page.\n',
	}
	notebook = t.setUpNotebook(content=content)

	step('full index update (indexing is always on now - no preference)')
	notebook.index.check_and_update()
	drain_main_loop()  # let the idle-driven icon scan finish

	step('check IconsView content')
	from zim.plugins.iconpages.indexer import IconsView
	view = IconsView.new_from_index(notebook.index)
	icon_home = view.get_icon('Home')
	icon_ambiguous = view.get_icon('Notes:Ambiguous')
	icon_none = view.get_icon('Notes')
	print('icon(Home) =', icon_home)
	print('icon(Notes:Ambiguous) =', icon_ambiguous)
	print('icon(Notes) =', icon_none)
	assert icon_home == 'home', icon_home
	from zim.plugins.iconpages.iconutils import SEVERAL_ICONS
	assert icon_ambiguous == SEVERAL_ICONS, icon_ambiguous
	assert icon_none is None, icon_none
	print('OK - indexer results correct')

	step('a bold run with nothing after it does not raise StopIteration')
	# find_text() calls next() on the same iterator the outer loop walks,
	# so an exhausted stream used to propagate StopIteration out of
	# _extract_icon() and abort indexing of the whole page.
	from zim.plugins.iconpages.indexer import IconsIndexer
	from zim.formats import STRONG
	result = IconsIndexer._extract_icon(iter([(STRONG, None)]))
	print('_extract_icon on a truncated bold run ->', result)
	assert result is False, result
	print('OK - truncated bold run handled')

	step('open main window (loads NotebookViewExtension + side pane widget)')
	mainwindow = setUpMainWindow(notebook, path='Home')
	ext = find_extension(mainwindow.pageview, __import__('zim.plugins.iconpages', fromlist=['x']).IconPagesNotebookViewExtension)
	print('OK - extension found:', ext)
	print('OK - widget:', ext.widget)

	step('exercise the tree model (walk all rows, read every column incl icon)')
	model = ext.widget.treeview.get_model()
	def walk(treeiter, depth=0):
		while treeiter is not None:
			name = model.get_value(treeiter, 0)
			icon = model.get_value(treeiter, ICON_COL)
			print('  ' * depth + '- %s (icon=%s)' % (name, 'yes' if icon is not None else 'no'))
			child = model.iter_children(treeiter)
			if child is not None:
				walk(child, depth + 1)
			treeiter = model.iter_next(treeiter)
	walk(model.get_iter_first())
	print('OK - tree walked without exceptions')

	step('default icons: folder for pages with children, file for leaves')
	def find_row_for_page(treeiter, target, depth=0):
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

	from zim.plugins.iconpages.iconutils import ICONS, FOLDER_ICON, FILE_ICON
	size = plugin.preferences['icon_size']
	expected_folder = ICONS.get_pixbuf(FOLDER_ICON, size)
	expected_file = ICONS.get_pixbuf(FILE_ICON, size)

	for pagename, expected, label in (
		('Projects', expected_folder, 'folder'),
		('Projects:Beta', expected_file, 'file'),
		('Projects:Alpha', expected_file, 'file'),
	):
		treeiter = find_row_for_page(model.get_iter_first(), pagename)
		assert treeiter is not None, pagename
		icon = model.get_value(treeiter, ICON_COL)
		print('  %-16s -> %s' % (pagename, label))
		assert icon is expected, '%s got the wrong default icon' % pagename
	print('OK - default icons follow "has children"')

	step('Insert Icon dialog builds without error')
	from zim.plugins.iconpages.panelview import InsertIconDialog
	insert_dialog = InsertIconDialog(mainwindow.pageview)
	names_in_model = [row[1] for row in insert_dialog.model]
	print('icons available in Insert Icon dialog:', names_in_model)
	assert 'calendar' in names_in_model
	insert_dialog._chosen = 'calendar'
	before = mainwindow.pageview.textview.get_buffer().get_text(
		mainwindow.pageview.textview.get_buffer().get_start_iter(),
		mainwindow.pageview.textview.get_buffer().get_end_iter(), True)
	insert_dialog.do_response_ok()
	after = mainwindow.pageview.textview.get_buffer().get_text(
		mainwindow.pageview.textview.get_buffer().get_start_iter(),
		mainwindow.pageview.textview.get_buffer().get_end_iter(), True)
	print('buffer before:', repr(before))
	print('buffer after :', repr(after))
	assert after != before and 'ICON=calendar' in after
	insert_dialog.destroy()
	print('OK - Insert Icon dialog inserted shortcode into buffer')

	del model, treeiter
	import gc; gc.collect()

	step('"Rescan Icons" action runs and keeps the same tree model')
	notebook_ext = find_extension(notebook, __import__('zim.plugins.iconpages', fromlist=['x']).IconPagesNotebookExtension)
	assert notebook_ext.indexer is not None, 'indexing should always be on'
	model_before = ext.widget.treeview.get_model()
	ext.rescan_icons()
	drain_main_loop()
	assert notebook_ext._backfill_source is None, 'rescan did not finish'
	assert ext.widget.treeview.get_model() is model_before, \
		'rescan replaced the tree model - it must only push per-page updates'
	view2 = IconsView.new_from_index(notebook.index)
	assert view2.get_icon('Home') == 'home', view2.get_icon('Home')
	print('OK - rescan completed, model untouched, icons still correct')

	step('remove plugin (full teardown drops the table)')
	PluginManager.remove_plugin('iconpages')
	assert notebook_ext.indexer is None
	try:
		IconsView.new_from_index(notebook.index)
		raise AssertionError('iconlist table should be gone after teardown')
	except ValueError:
		pass
	print('OK - plugin removed and its table dropped')

	print('\nALL CHECKS PASSED')

except Exception:
	traceback.print_exc()
	sys.exit(1)
