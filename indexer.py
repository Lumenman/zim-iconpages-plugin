# -*- coding: utf-8 -*-

# Released under the GNU GPL version 3.
# This is a plugin for the Zim desktop wiki (zim-wiki.org).

'''Content indexer that keeps a small side-table in Zim's index database
mapping page name -> icon name, based on the "[ICON=name]" shortcode found
in the page text (see L{iconutils}).
'''

import logging
import sqlite3

from zim.parse.tokenlist import TEXT, skip_to_end_token
from zim.formats import STRONG
from zim.notebook.index.base import IndexerBase, IndexView

from .iconutils import SEVERAL_ICONS, ICON_RE

logger = logging.getLogger('zim.plugins.iconpages')


class IconsIndexer(IndexerBase):
	'''Indexer that gets added to the notebook L{Index} to keep track of
	the icon shortcode per page.

	The result is stored in its own table: one row per page that has an
	icon shortcode, C{icon} is either a single icon name, or the special
	value L{SEVERAL_ICONS} when more than one shortcode was found on the
	page (ambiguous - the page tree view falls back to a "several icons"
	placeholder in that case).
	'''

	PLUGIN_NAME = 'iconpages'
	PLUGIN_DB_FORMAT = '0.9'  # bump this if the table layout changes

	#: Key this plugin used before it was renamed from "IconTags". The
	#: table itself ("iconlist") is unchanged, so nothing needs to be
	#: rebuilt - but the old key would sit in "zim_index" forever with
	#: nobody left to read it, so it is cleaned up on startup.
	LEGACY_PLUGIN_NAME = 'icontags'

	INIT_SCRIPT = '''
		CREATE TABLE IF NOT EXISTS iconlist (
			id TEXT PRIMARY KEY,
			icon TEXT
		);
		INSERT OR REPLACE INTO zim_index VALUES (%r, %r);
	''' % (PLUGIN_NAME, PLUGIN_DB_FORMAT)

	TEARDOWN_SCRIPT = '''
		DROP TABLE IF EXISTS "iconlist";
		DELETE FROM zim_index WHERE key = %r;
		DELETE FROM zim_index WHERE key = %r;
	''' % (PLUGIN_NAME, LEGACY_PLUGIN_NAME)

	# define signals we want to use - (closure type, return type and arg types)
	__signals__ = {'iconlist-changed': (None, None, (object,))}

	@classmethod
	def new_from_index(cls, index):
		db = index._db
		pagesindexer = index.update_iter.pages
		return cls(db, pagesindexer)

	def __init__(self, db, pagesindexer):
		IndexerBase.__init__(self, db)

		self.db.executescript(self.INIT_SCRIPT)

		self.connectto_all(pagesindexer, (
			'page-changed', 'page-row-deleted'
		))

	def on_page_changed(self, o, row, doc):
		new_icon = self._extract_icon(doc.iter_tokens())
		if not new_icon:
			self._ind_remove(row['name'])
		else:
			self._ind_insert(row['name'], new_icon)

	def on_page_row_deleted(self, o, row):
		self._ind_remove(row['name'])

	def drop_orphans(self):
		'''Delete rows for pages that are no longer in the index.

		Normally C{on_page_row_deleted()} keeps the table in step, so
		this is only a safety net for rows that were somehow missed
		(e.g. pages deleted while the plugin was disabled). Used at the
		end of a full rescan.

		@returns: the number of rows deleted
		'''
		cursor = self.db.cursor()
		cursor.execute(
			'DELETE FROM iconlist WHERE id NOT IN (SELECT name FROM pages)')
		return cursor.rowcount

	def _ind_insert(self, pagename, icon):
		'''Insert (update) the icon for C{pagename} in the index.

		Like L{_ind_remove()}, this only emits C{iconlist-changed} when
		the stored value actually changed - re-writing the same icon on
		every save of the page would otherwise make every side pane
		widget redraw that row for nothing.
		'''
		try:
			cursor = self.db.cursor()
			row = cursor.execute(
				'SELECT icon FROM iconlist WHERE id = ?', (pagename,)
			).fetchone()
			if row is not None and row[0] == icon:
				return
			cursor.execute(
				'''
				INSERT OR REPLACE INTO iconlist (id, icon)
				VALUES (?, ?)''', (pagename, icon))
			self.emit('iconlist-changed', pagename)
		except sqlite3.Error:
			logger.exception('IconPages: error while indexing icon for page: %s', pagename)

	def _ind_remove(self, pagename):
		cursor = self.db.cursor()
		count, = cursor.execute(
			'SELECT count(*) FROM iconlist WHERE id = ?',
			(pagename,)
		).fetchone()
		if count > 0:
			cursor.execute(
				'DELETE FROM iconlist WHERE id = ?',
				(pagename,)
			)
			self.emit('iconlist-changed', pagename)

	@staticmethod
	def _extract_icon(tokens):
		'''Look for "[ICON=name]" shortcodes inside bold ("strong") text.
		@returns: an icon name (case preserved as written on the page),
		L{SEVERAL_ICONS} if more than one case-insensitively distinct
		name was found, or C{False} if none was found.
		'''
		def find_text(token_iter):
			# NOTE: the default for next() is required. This runs inside
			# the for-loop below, over the *same* iterator, and a bare
			# next() on an exhausted iterator raises StopIteration that
			# the for statement does not swallow - it would propagate out
			# and break indexing of the page. An empty bold run ("****"
			# at the very end of a page) is enough to trigger that.
			token = next(token_iter, None)
			text = token[1] if token is not None and token[0] == TEXT else ''
			skip_to_end_token(token_iter, STRONG)
			return text

		found_names = []  # preserves order and original case
		token_iter = iter(tokens)

		for token in token_iter:
			if token[0] != STRONG:
				continue
			text = find_text(token_iter)
			found_names.extend(ICON_RE.findall(text))

		if not found_names:
			return False

		# Matching is case-insensitive (see IconStore), so the same icon
		# name typed with different capitalization in more than one place
		# on the page is not treated as ambiguous.
		if len(set(name.lower() for name in found_names)) > 1:
			return SEVERAL_ICONS

		return found_names[0]


class IconsView(IndexView):
	'''Read-only "view" on the index to look up the icon for a page.'''

	def __init__(self, db):
		IndexView.__init__(self, db)

		# Confirm the table is actually there (indexing may be disabled).
		try:
			db.execute('SELECT * FROM iconlist LIMIT 1')
		except sqlite3.OperationalError:
			raise ValueError('No "iconlist" table in the index - icon indexing is not enabled')

	def get_icon(self, pagename):
		'''Returns the icon name stored for C{pagename}, or C{None}.'''
		cursor = self.db.cursor()
		cursor.execute('SELECT icon FROM iconlist WHERE id = ?', (pagename,))
		result = cursor.fetchone()
		return result[0] if result else None