'''Regression test: the IconPages tree must expand and collapse its
branches exactly like the built-in Page Index tree ("Содержание") as the
user moves between pages.

Both panes are opened on the same window at the same time and fed the
same sequence of page switches; after every switch the set of expanded
rows and the current selection must match.

The bug this guards against: IconPagesPluginWidget.set_page() used to
call treeview.select_treepath() a second time on top of what
PageTreeView.set_current_page() had already done. select_treepath() also
calls _store_expanded_path(), which records the row to collapse back to
on the next switch - so running it again, after the branch had just been
expanded, stored the already-expanded row as the restore point and the
branch then stayed open where the built-in tree closed it.
'''
import sys, traceback
import tests, zim.config, zim.plugins
from zim.plugins import PluginManager, find_extension
from zim.notebook import Path

zim.config.manager.makeConfigManagerVirtual()
zim.plugins.resetPluginManager()
from tests.mainwindow import setUpMainWindow

iconpages = __import__('zim.plugins.iconpages', fromlist=['x'])
pageindex = __import__('zim.plugins.pageindex', fromlist=['x'])


def drain_main_loop():
	'''Run any pending GLib idle/timeout callbacks.

	The plugin's icon scan runs in chunks from an idle handler so it
	cannot freeze the UI on a large notebook. These tests never spin a
	main loop of their own, so they have to drain it explicitly.
	'''
	from gi.repository import GLib
	ctx = GLib.MainContext.default()
	while ctx.pending():
		ctx.iteration(False)


def step(name):
	print('--- %s' % name)


class T(tests.TestCase):
	def runTest(self):
		pass

t = T()
t.setUpClass()

try:
	step('load both the built-in Page Index and IconPages')
	PluginManager.load_plugin('pageindex')
	PluginManager.load_plugin('iconpages')

	# Deep enough on several branches that auto-expand/auto-collapse has
	# something to actually do when moving between them.
	content = {
		'Alpha': 'a\n',
		'Alpha:One': 'a1\n',
		'Alpha:One:Deep': '**[ICON=home]**\n',
		'Beta': 'b\n',
		'Beta:Two': 'b2\n',
		'Beta:Two:Deeper': 'b2d\n',
		'Gamma': 'g\n',
		'Gamma:Three': 'g3\n',
	}
	notebook = t.setUpNotebook(content=content)
	notebook.index.check_and_update()
	drain_main_loop()

	mainwindow = setUpMainWindow(notebook, path='Alpha:One:Deep')
	drain_main_loop()

	ext_ip = find_extension(mainwindow.pageview, iconpages.IconPagesNotebookViewExtension)
	ext_pi = find_extension(mainwindow.pageview, pageindex.PageIndexNotebookViewExtension)
	assert ext_ip is not None, 'IconPages extension not found'
	assert ext_pi is not None, 'Page Index extension not found'

	tv_ip = ext_ip.widget.treeview
	tv_pi = ext_pi.treeview

	step('both trees use the same autoexpand/autocollapse settings')
	print('  IconPages : autoexpand=%s autocollapse=%s' % (tv_ip._autoexpand, tv_ip._autocollapse))
	print('  Page Index: autoexpand=%s autocollapse=%s' % (tv_pi._autoexpand, tv_pi._autocollapse))
	assert (tv_ip._autoexpand, tv_ip._autocollapse) == (tv_pi._autoexpand, tv_pi._autocollapse), \
		'the two trees are configured differently - the comparison below would be meaningless'

	def expanded(treeview):
		out = []
		treeview.map_expanded_rows(lambda tv, path, data=None: out.append(path.to_string()))
		return sorted(out)

	def selected(treeview):
		path = treeview.get_selected_path()
		return path.name if path is not None else None

	def compare(label):
		e_ip, e_pi = expanded(tv_ip), expanded(tv_pi)
		s_ip, s_pi = selected(tv_ip), selected(tv_pi)
		print('  %-18s expanded: IconPages=%-14s PageIndex=%-14s | selected: %s / %s'
			% (label, e_ip, e_pi, s_ip, s_pi))
		assert e_ip == e_pi, \
			'expanded rows differ after %s: IconPages=%r Page Index=%r' % (label, e_ip, e_pi)
		assert s_ip == s_pi, \
			'selection differs after %s: IconPages=%r Page Index=%r' % (label, s_ip, s_pi)

	step('state right after load (before any switch)')
	compare('on load')

	step('walk through pages and compare after every switch')
	# Deliberately mixes deep->deep jumps across branches (the case that
	# exercises collapse-then-expand), moves up to a parent, and revisits
	# a branch that was already open.
	sequence = [
		'Beta:Two:Deeper',
		'Alpha:One:Deep',
		'Gamma:Three',
		'Alpha',
		'Beta:Two',
		'Beta:Two:Deeper',
		'Gamma',
		'Alpha:One:Deep',
	]
	for pagename in sequence:
		mainwindow.open_page(Path(pagename))
		drain_main_loop()
		compare('open %s' % pagename)

	step('a preferences change must not break the sync')
	# Unlike Page Index, this widget rebuilds its tree model when the
	# icon size preference changes. The rebuilt model has no current
	# page, the treeview's auto-expand bookkeeping still points into the
	# old model, and set_model() drops the expanded state of every row -
	# so without explicit repair the two trees drift apart from here on.
	# A 'changed' emission with nothing actually edited (a bare OK in
	# the "Configure Plugin" dialog, or preferences.conf re-read by the
	# gio file monitor after *any* rewrite of that file) must not
	# rebuild anything at all.
	mainwindow.open_page(Path('Alpha:One:Deep'))
	drain_main_loop()
	compare('before preferences change')

	model_before = ext_ip.widget.treeview.get_model()
	ext_ip.widget.plugin.preferences.emit('changed')  # as if OK was pressed
	drain_main_loop()
	assert ext_ip.widget.treeview.get_model() is model_before, \
		'model was rebuilt on a spurious preferences "changed" (nothing was edited)'
	compare('right after preferences change')

	ext_ip.widget.plugin.preferences['icon_size'] = 24  # a real change
	drain_main_loop()
	assert ext_ip.widget.treeview.get_model() is not model_before, \
		'model was not rebuilt on a real icon_size change'
	compare('after a real icon_size change')

	for pagename in ('Beta:Two:Deeper', 'Gamma:Three', 'Alpha:One:Deep'):
		mainwindow.open_page(Path(pagename))
		drain_main_loop()
		compare('open %s (post-reload)' % pagename)

	step('manual expand is respected the same way by both trees')
	# A branch the user opened by hand must not be auto-collapsed - both
	# trees rely on get_any_children_expanded() for that.
	from gi.repository import Gtk
	tv_ip.expand_all()
	tv_pi.expand_all()
	mainwindow.open_page(Path('Gamma:Three'))
	drain_main_loop()
	compare('after expand_all')

	step('manually expanded branches survive a model rebuild')
	# The built-in tree never rebuilds its model, so branches the user
	# opened by hand stay open there no matter what. Our rebuild must
	# capture and re-expand them, or the trees diverge exactly when the
	# user has invested the most state into the tree.
	model_before = tv_ip.get_model()
	ext_ip.widget.plugin.preferences.emit('changed')  # spurious - no rebuild
	drain_main_loop()
	assert tv_ip.get_model() is model_before, \
		'model was rebuilt on a spurious preferences "changed" (nothing was edited)'
	compare('spurious changed while expanded')

	ext_ip.widget.plugin.preferences['icon_size'] = 32  # real change - rebuild
	drain_main_loop()
	assert tv_ip.get_model() is not model_before, \
		'model was not rebuilt on a real icon_size change'
	compare('icon_size change while expanded by hand')

	for pagename in ('Beta:Two:Deeper', 'Alpha:One:Deep'):
		mainwindow.open_page(Path(pagename))
		drain_main_loop()
		compare('open %s (after rebuild while expanded)' % pagename)

	step('view options apply live and do not touch the model')
	# The plugin mirrors the Page Index view options (autoexpand,
	# autocollapse, drag&drop, hscroll, tooltips, tree lines). These are
	# treeview-level setters: they must be applied on a preferences
	# change without a model rebuild, or every such change would
	# collapse the tree.
	model_now = tv_ip.get_model()
	ext_ip.widget.plugin.preferences['tree_lines'] = True
	drain_main_loop()
	assert tv_ip.get_model() is model_now, \
		'a view-level option rebuilt the model - it must only be re-applied to the view'
	assert tv_ip.get_enable_tree_lines(), 'tree_lines=True was not applied to the view'

	ext_ip.widget.plugin.preferences['use_tooltip'] = False
	drain_main_loop()
	assert tv_ip.get_model() is model_now, \
		'a view-level option rebuilt the model - it must only be re-applied to the view'
	assert tv_ip.get_tooltip_column() == -1, 'use_tooltip=False was not applied to the view'

	ext_ip.widget.plugin.preferences['tree_lines'] = False
	ext_ip.widget.plugin.preferences['use_tooltip'] = True
	drain_main_loop()
	compare('after view-only option changes')

	step('cleanup')
	PluginManager.remove_plugin('iconpages')
	PluginManager.remove_plugin('pageindex')

	print('\nALL TREE-PARITY CHECKS PASSED')
except Exception:
	traceback.print_exc()
	sys.exit(1)
