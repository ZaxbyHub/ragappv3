"""
SQL/DDL schema parser for extracting searchable SQL statements.
Parses .sql and .ddl files: CREATE TABLE definitions as structured
table chunks, plus every other top-level statement (views, indexes,
inserts, procedures, ...) as verbatim other-SQL chunks so no SQL
construct is silently dropped (issue #703).
"""

import codecs
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class SchemaParser:
    """
    Parser for SQL/DDL schema files.

    Extracts CREATE TABLE blocks from .sql and .ddl files,
    capturing table names and column definitions.
    """

    # Maximum file size in bytes (100MB)
    MAX_FILE_SIZE = 100 * 1024 * 1024

    # Regex pattern to match CREATE TABLE blocks.
    # Handles optional VIRTUAL keyword, IF NOT EXISTS, qualified schema
    # prefixes, and quoted identifiers ("name with spaces", `backticked`,
    # 'single-quoted') for both the schema prefix and the table name —
    # bare names keep the conventional [A-Za-z_]\w* form. The terminator
    # allows whitespace/newlines between the closing ')' and the ';'
    # (issue #513 W5 / RC-12). The non-greedy column capture stops at the
    # FIRST ')\s*;' after each CREATE, so multi-statement files yield one
    # match per block.
    #
    # Capture groups:
    #   1-4: optional schema prefix (double-quoted / backticked /
    #        single-quoted / bare), each holding the prefix's inner text
    #   5-8: table name (double-quoted / backticked / single-quoted / bare),
    #        each holding the identifier's inner (unquoted) text
    _QUOTED_IDENTIFIER = r'(?:"([^"]+)"|`([^`]+)`|\'([^\']+)\'|([A-Za-z_][\w$]*))'
    CREATE_TABLE_PATTERN = re.compile(
        r'CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
        r'(?:' + _QUOTED_IDENTIFIER + r'\s*\.\s*)?'
        + _QUOTED_IDENTIFIER
        + r'\s*\((.*?)\)\s*;',
        re.IGNORECASE | re.DOTALL
    )

    # Valid file extensions
    VALID_EXTENSIONS = {'.sql', '.ddl'}

    # Byte-order marks that unambiguously identify UTF-16/UTF-32 text
    # before any codec guesswork runs (issue #703 / T1-02-K-06).
    _BOM_CODECS = (
        (codecs.BOM_UTF32_LE, 'utf-32'),
        (codecs.BOM_UTF32_BE, 'utf-32'),
        (codecs.BOM_UTF8, 'utf-8-sig'),
        (codecs.BOM_UTF16_LE, 'utf-16'),
        (codecs.BOM_UTF16_BE, 'utf-16'),
    )

    @classmethod
    def _decode(cls, data: bytes) -> str:
        """Decode schema-file bytes with a real-world encoding chain.

        Order: an unambiguous BOM wins; otherwise strict UTF-8 (covering
        plain ASCII/UTF-8 and BOM'd UTF-8); on failure a lossy cp1252
        leg — the encoding Windows tools most commonly export — with
        ``errors='replace'`` as the final fallback so undecodable bytes
        degrade characters instead of failing the whole file. UTF-16
        without a BOM is not detected (not distinguishable with
        confidence) and remains out of scope.
        """
        for bom, codec in cls._BOM_CODECS:
            if data.startswith(bom):
                return data.decode(codec)
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return data.decode('cp1252', errors='replace')

    def parse(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Parse a SQL/DDL file and extract CREATE TABLE definitions.

        Args:
            file_path: Path to the .sql or .ddl file

        Returns:
            List of chunk dictionaries with 'text' and 'metadata' keys

        Raises:
            FileNotFoundError: If the file does not exist
            ValueError: If the file has an invalid extension
        """
        path = Path(file_path)

        # Validate file exists
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Validate file extension
        if path.suffix.lower() not in self.VALID_EXTENSIONS:
            raise ValueError(
                f"Invalid file extension '{path.suffix}'. "
                f"Expected one of: {', '.join(self.VALID_EXTENSIONS)}"
            )

        # Check file size
        file_size = path.stat().st_size
        if file_size > self.MAX_FILE_SIZE:
            raise ValueError(
                f"File size {file_size} bytes exceeds maximum allowed size "
                f"of {self.MAX_FILE_SIZE} bytes (100MB)"
            )

        # Read and decode with the BOM-aware fallback chain (issue #703:
        # a strict-UTF-8-only read made UTF-16 .sql files yield zero
        # chunks through the lossy replace fallback).
        content = self._decode(path.read_bytes())

        return self._extract_chunks(content, source_file=str(path))

    def parse_text(self, sql_text: str) -> List[Dict[str, Any]]:
        """
        Parse SQL/DDL text directly (without reading from file).

        Args:
            sql_text: SQL/DDL content as string

        Returns:
            List of chunk dictionaries with 'text' and 'metadata' keys
        """
        # Handle empty or whitespace-only input
        if not sql_text or not sql_text.strip():
            return []

        return self._extract_chunks(sql_text, source_file=None)

    @staticmethod
    def _identifier_parts(match: "re.Match") -> tuple:
        """Extract (bare_table_name, original_spelling) from a pattern match.

        The table name may be quoted (``"order items"``, ``'order items'``,
        backticked) or bare. The ORIGINAL quoted spelling — including any
        qualified schema prefix — is preserved for the emitted chunk text so
        definitions round-trip (re-parsing the emitted text yields the same
        identifier); the bare (unquoted) table identifier is returned for
        metadata.

        Returns:
            Tuple of (bare_table_name, original_spelling).
        """
        # Groups 1-4: optional schema prefix (double-quoted/backtick/single/
        # bare). Groups 5-8: table name in the same four spellings.
        prefix_quote, prefix_bare = None, None
        if match.group(1) is not None:
            prefix_quote, prefix_bare = '"', match.group(1)
        elif match.group(2) is not None:
            prefix_quote, prefix_bare = '`', match.group(2)
        elif match.group(3) is not None:
            prefix_quote, prefix_bare = "'", match.group(3)
        elif match.group(4) is not None:
            prefix_quote, prefix_bare = '', match.group(4)

        name_quote, bare_name = '', ''
        if match.group(5) is not None:
            name_quote, bare_name = '"', match.group(5)
        elif match.group(6) is not None:
            name_quote, bare_name = '`', match.group(6)
        elif match.group(7) is not None:
            name_quote, bare_name = "'", match.group(7)
        elif match.group(8) is not None:
            name_quote, bare_name = '', match.group(8)

        original = ''
        if prefix_bare is not None:
            original = f'{prefix_quote}{prefix_bare}{prefix_quote}.'
        original += f'{name_quote}{bare_name}{name_quote}'
        return bare_name, original

    # Leading keyword(s) used only to label residual statements.
    _STATEMENT_TYPE = re.compile(
        r'^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|VIEW|INDEX|TRIGGER|PROCEDURE'
        r'|FUNCTION)|INSERT|UPDATE|DELETE|ALTER|DROP|GRANT|REVOKE|WITH|SET)\b',
        re.IGNORECASE,
    )

    @staticmethod
    def _statement_type(statement: str) -> str:
        """Best-effort label for a non-CREATE TABLE statement's first keyword."""
        match = SchemaParser._STATEMENT_TYPE.match(statement)
        if match is None:
            return 'OTHER'
        # Group 1 holds the CREATE object word (VIEW/INDEX/...); otherwise
        # the matched leading verb itself labels the statement.
        return (match.group(1) or match.group(0)).strip().split()[-1].upper()

    # Quote openers whose spans protect comment markers inside them —
    # the same three spellings _QUOTED_IDENTIFIER accepts ('...' strings
    # and "..." / `...` quoted identifiers), each with a doubled-quote
    # escape so 'a''b' stays one span.
    _QUOTE_CLOSER = {"'": "'", '"': '"', '`': '`'}

    @classmethod
    def _strip_comments(cls, text: str) -> str:
        """Remove -- line comments and /* */ block comments, quote-aware.

        Comment markers inside ANY quoted span — '...' strings, "..."
        and `...` quoted identifiers — are content, not comments, and
        survive verbatim (a doubled quote escapes inside its span).
        Comment markers inside comments are consumed with their comment.
        """
        out: List[str] = []
        i, n = 0, len(text)
        closer = ''
        while i < n:
            ch = text[i]
            if closer:
                out.append(ch)
                if ch == closer:
                    if i + 1 < n and text[i + 1] == closer:
                        out.append(closer)
                        i += 2
                        continue
                    closer = ''
                i += 1
                continue
            if ch in cls._QUOTE_CLOSER:
                closer = ch
                out.append(ch)
                i += 1
                continue
            if ch == '-' and i + 1 < n and text[i + 1] == '-':
                newline = text.find('\n', i)
                i = n if newline == -1 else newline
                continue
            if ch == '/' and i + 1 < n and text[i + 1] == '*':
                close = text.find('*/', i + 2)
                i = n if close == -1 else close + 2
                out.append(' ')
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    @classmethod
    def _split_statements(cls, text: str) -> List[str]:
        """Split SQL text on ';' OUTSIDE quoted spans.

        Same quote model as _strip_comments ('...', "...", `...` with
        doubled-quote escapes), so a semicolon inside any literal no
        longer splits a statement's chunk boundary.
        """
        statements: List[str] = []
        current: List[str] = []
        i, n = 0, len(text)
        closer = ''
        while i < n:
            ch = text[i]
            if closer:
                current.append(ch)
                if ch == closer:
                    if i + 1 < n and text[i + 1] == closer:
                        current.append(closer)
                        i += 2
                        continue
                    closer = ''
                i += 1
                continue
            if ch in cls._QUOTE_CLOSER:
                closer = ch
                current.append(ch)
                i += 1
                continue
            if ch == ';':
                statements.append(''.join(current))
                current = []
                i += 1
                continue
            current.append(ch)
            i += 1
        statements.append(''.join(current))
        return statements

    def _extract_chunks(
        self, content: str, source_file: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Extract CREATE TABLE chunks plus every other SQL statement.

        CREATE TABLE blocks keep their structured extraction (original
        quoted spelling round-trips; metadata carries the bare table
        name). Comments are stripped (quote-aware) once up front, and
        BOTH the CREATE TABLE match and the residual statement split run
        on the stripped text — a commented-out CREATE TABLE can no
        longer mint a phantom table chunk. Every other top-level
        statement in the residual — views, inserts, indexes,
        procedures, ... — becomes an ``other_sql`` chunk carrying the
        statement's whitespace-normalized text, so no SQL construct is
        silently dropped (issue #703 / T1-02-K-05). A semicolon inside a
        comment can neither create a chunk nor merge two statements, and
        the statement split itself is quote-aware, so a semicolon inside
        any literal keeps its statement whole. A file whose residual is
        only comments/whitespace yields zero chunks.
        """
        chunks = []

        # Strip comments once (quote-aware); every extraction below runs
        # on the stripped text so commented-out SQL is never extracted.
        content = self._strip_comments(content)

        # Find all CREATE TABLE blocks
        table_spans: List[tuple] = []
        for match in self.CREATE_TABLE_PATTERN.finditer(content):
            table_spans.append((match.start(), match.end()))
            bare_name, original_name = self._identifier_parts(match)
            column_block = match.group(9).strip()

            # Reconstruct the full table definition using the original
            # identifier spelling so the definition round-trips.
            table_definition = f"CREATE TABLE {original_name} (\n{column_block}\n);"

            chunk = {
                'text': table_definition,
                'metadata': {
                    'table_name': bare_name,
                    'object_type': 'table',
                    'source_file': source_file
                }
            }
            chunks.append(chunk)

        # Blank out the matched table blocks, then emit every remaining
        # top-level statement (issue #703: nothing is dropped, and
        # comments never masquerade as statements).
        residual_parts: List[str] = []
        cursor = 0
        for start, end in table_spans:
            residual_parts.append(content[cursor:start])
            cursor = end
        residual_parts.append(content[cursor:])
        residual = ''.join(residual_parts)

        for statement in self._split_statements(residual):
            if not statement.strip():
                continue
            text = ' '.join(statement.split())
            chunks.append({
                'text': text + ';',
                'metadata': {
                    'statement_type': self._statement_type(statement),
                    'object_type': 'other_sql',
                    'source_file': source_file,
                },
            })

        return chunks
