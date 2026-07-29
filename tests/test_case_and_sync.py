'''Regression test for two fixes:
  1. Case-insensitive icon name matching (a shortcode like
     "[ICON=Attachment]" must match a file named "attachment.png",
     "Attachment.png", etc., and vice versa).
  2. The side panel tree must reveal/select the currently open page
     immediately when the plugin loads, matching the built-in Page
     Index ("Содержание") panel - not only after the next page switch.
'''
import sys, traceback, os, shutil
import tests, zim.config, zim.plugins
from zim.plugins import PluginManager, find_extension
from zim.notebook import Path

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
	# --- Part 1: case-insensitive icon matching ---------------------
	from zim.plugins.iconpages.iconutils import ICONS, IconStore

	# Simulate a user-added icon file with a capital letter in the name
	# (e.g. as reported: works fine for "calendar", "some-word", but
	# breaks for "Attachment").
	tmp_dir = '/tmp/iconpages_case_test_icons'
	if os.path.exists(tmp_dir):
		shutil.rmtree(tmp_dir)
	os.makedirs(tmp_dir)
	shutil.copy(ICONS.icons_dir + '/attachment.png', tmp_dir + '/Attachment.png')
	shutil.copy(ICONS.icons_dir + '/next.png', tmp_dir + '/multi-word-name.png')

	store = IconStore(tmp_dir)
	assert store.has_icon('Attachment')
	assert store.has_icon('attachment'), 'lowercase lookup must match capitalized file'
	assert store.has_icon('ATTACHMENT'), 'uppercase lookup must match capitalized file'
	assert store.get_pixbuf('attachment', 16) is not None
	assert store.get_pixbuf('ATTACHMENT', 16) is not None
	assert store.has_icon('multi-word-name')
	assert store.has_icon('Multi-Word-Name')
	print('OK - IconStore matches icon names case-insensitively')

	# --- Part 1b: SVG support ---------------------------------------
	from zim.plugins.iconpages.iconutils import _check_svg_support, ICON_EXTENSIONS

	SVG = '''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <rect x="1" y="1" width="14" height="14" rx="2" fill="#3striped"/>
</svg>
'''.replace('#3striped', '#3388cc')

	svg_dir = tmp_dir + '_svg'
	if os.path.exists(svg_dir):
		shutil.rmtree(svg_dir)
	os.makedirs(svg_dir)
	with open(svg_dir + '/vector.svg', 'w') as fh:
		fh.write(SVG)
	with open(svg_dir + '/Both.SVG', 'w') as fh:  # capitalized name AND extension
		fh.write(SVG)
	shutil.copy(ICONS.icons_dir + '/home.png', svg_dir + '/Both.png')
	shutil.copy(ICONS.icons_dir + '/home.png', svg_dir + '/raster.png')
	with open(svg_dir + '/.hidden.png', 'w') as fh:
		fh.write('not an icon')

	svg_store = IconStore(svg_dir)
	names = svg_store.list_names()
	print('SVG loader available:', _check_svg_support())
	print('names found:', names)

	assert 'raster.png' not in names and 'raster' in names, names
	assert '.hidden' not in names, 'dotfiles must not be offered as icons'

	if _check_svg_support():
		assert 'vector' in names, names
		assert svg_store.has_icon('VECTOR'), 'svg lookup must be case-insensitive too'
		pixbuf = svg_store.get_pixbuf('vector', 48)
		assert pixbuf is not None, 'svg failed to render'
		# Rendered, not upscaled: an SVG asked for at 48px really is 48px.
		assert pixbuf.get_width() == 48 and pixbuf.get_height() == 48, \
			(pixbuf.get_width(), pixbuf.get_height())
		# svg wins over png for the same icon name
		assert svg_store._files['both'].lower().endswith('.svg'), svg_store._files['both']
		print('OK - SVG icons load, render at the requested size, and win over PNG')
	else:
		# No librsvg: svg files must be ignored quietly, and a png with
		# the same name must still be usable.
		assert 'vector' not in names, 'svg must be ignored without a loader'
		assert svg_store._files['both'].lower().endswith('.png'), svg_store._files['both']
		print('OK - no SVG loader here; svg ignored and png fallback used')

	assert ICON_EXTENSIONS[0] == '.svg', 'svg must have priority over png'

	plugin = PluginManager.load_plugin('iconpages')

	content = {
		'CapitalIcon': '**[ICON=Attachment]**\n',
		'LowerIcon': '**[ICON=attachment]**\n',
		'MixedRepeat': 'text **[ICON=Attachment]** and again **[ICON=attachment]**\n',
			# same icon, different case, repeated - must NOT count as ambiguous
	}
	notebook = t.setUpNotebook(content=content)
	notebook.index.check_and_update()
	drain_main_loop()  # let the idle-driven initial icon scan finish

	from zim.plugins.iconpages.indexer import IconsView
	from zim.plugins.iconpages.iconutils import ICONS as REAL_ICONS, SEVERAL_ICONS
	view = IconsView.new_from_index(notebook.index)

	icon1 = view.get_icon('CapitalIcon')
	icon2 = view.get_icon('LowerIcon')
	icon3 = view.get_icon('MixedRepeat')
	print('CapitalIcon ->', repr(icon1), '| pixbuf found:', REAL_ICONS.get_pixbuf(icon1, 16) is not None)
	print('LowerIcon ->', repr(icon2), '| pixbuf found:', REAL_ICONS.get_pixbuf(icon2, 16) is not None)
	print('MixedRepeat ->', repr(icon3))

	assert REAL_ICONS.get_pixbuf(icon1, 16) is not None, 'capitalized shortcode must resolve to a real icon'
	assert REAL_ICONS.get_pixbuf(icon2, 16) is not None
	assert icon3 != SEVERAL_ICONS, 'same icon repeated with different case must not be "ambiguous"'
	print('OK - real icon indexing is case-insensitive end to end')

	# --- Part 2: side panel reveals the current page immediately ----
	content2 = {
		'Alpha': 'a\n',
		'Alpha:Beta': 'b\n',
		'Alpha:Beta:Gamma': 'g\n',
	}
	notebook2 = t.setUpNotebook(name='sync_test_notebook', content=content2)
	mainwindow = setUpMainWindow(notebook2, path='Alpha:Beta:Gamma')
		# NB: setUpMainWindow already opens this page BEFORE our extension
		# is created, same as a real app startup with a remembered page.

	ext = find_extension(mainwindow.pageview, iconpages.IconPagesNotebookViewExtension)
	selected = ext.widget.treeview.get_selected_path()
	print('selected path in IconPages tree right after load:', selected)
	assert selected is not None, 'tree should already have a selection on load'
	assert selected.name == 'Alpha:Beta:Gamma', selected

	# also check it is actually revealed (not just "selected" internally
	# with collapsed parents)
	treeview = ext.widget.treeview
	model = treeview.get_model()
	treepaths = model.find_all(Path('Alpha:Beta:Gamma'))
	assert treepaths, 'page not found in the model'
	treepath = treepaths[0]
	parent = treepath.copy()
	while parent.get_depth() > 1:
		parent.up()
		assert treeview.row_expanded(parent), \
			'parent row %s is collapsed - the page is not actually visible' % parent.to_string()
	print('OK - IconPages tree already shows/selects the current page on load')

	print('\nALL CASE+SYNC CHECKS PASSED')
except Exception:
	traceback.print_exc()
	sys.exit(1)
