"""
SQL/DDL schema parser for extracting searchable SQL statements.
Parses .sql and .ddl files: CREATE TABLE definitions as structured
table chunks, plus every other top-level statement (views, indexes,
inserts, procedures, ...) as whitespace-normalized other-SQL chunks so
no SQL construct is silently dropped (issue #703).
"""

import codecs
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def raise_for_suspicious_decode(text: str, *, what: str) -> None:
    """Reject a decoded payload that is almost certainly mis-decoded.

    Two gates shared by the schema and CSV decode chains (issue #703
    review): a replacement-character ratio above 1% means the lossy
    legacy leg mangled a non-cp1252 encoding (mojibake success is worse
    than an honest failure), and a NUL ratio above 10% means binary
    content or BOM-less UTF-16 leaked through as interleaved-NUL text.
    Raises ``ValueError`` naming ``what``; callers wrap it into their
    own parse-error type.
    """
    if not text:
        return
    n = len(text)
    if text.count("\ufffd") * 100 > n:
        raise ValueError(
            f"{what} is not valid UTF-8 or a supported legacy encoding "
            "(too many undecodable bytes)"
        )
    if text.count("\x00") * 10 > n:
        raise ValueError(
            f"{what} looks like binary content or BOM-less UTF-16, not text"
        )


class SchemaParser:
    """
    Parser for SQL/DDL schema files.

    Emits a structured chunk per CREATE TABLE block (original quoted
    identifier spelling round-trips; metadata carries the bare table
    name) plus one whitespace-normalized ``other_sql`` chunk per other
    top-level statement — views, inserts, indexes, procedures, ... —
    so no SQL construct is silently dropped (issue #703). Comments
    (``--``, ``/* */``, MySQL ``#``) never become chunks, and
    PostgreSQL dollar-quoted bodies ($$...$$, $tag$...$tag$) stay one
    statement.
    """

    # Maximum file size in bytes (100MB)
    MAX_FILE_SIZE = 100 * 1024 * 1024

    # A CREATE TABLE column block longer than this is emitted as a plain
    # other_sql statement instead of a structured table chunk. The bound
    # keeps the lazy capture linear-bounded per attempt (issue #703
    # review: the unbounded DOTALL scan was quadratic on unterminated
    # input), and a >1MB column block has no retrieval value as a
    # "table" anyway — the content is still indexed verbatim.
    MAX_COLUMN_BLOCK_CHARS = 1_000_000

    # Upper bound on statements extracted from one file. A 100MB dump of
    # one-INSERT-per-line statements would otherwise mint millions of
    # chunks and overwhelm the embedding pipeline downstream (issue #703
    # review); exceeding the cap fails the file with an accurate message
    # instead.
    MAX_STATEMENT_CHUNKS = 20_000

    # Regex pattern to match CREATE TABLE blocks.
    # Handles optional VIRTUAL keyword, IF NOT EXISTS, qualified schema
    # prefixes, and quoted identifiers ("name with spaces", `backticked`,
    # 'single-quoted') for both the schema prefix and the table name —
    # bare names keep the conventional [A-Za-z_]\w* form. The terminator
    # allows whitespace/newlines between the closing ')' and the ';'
    # (issue #513 W5 / RC-12). The column capture is bounded to
    # MAX_COLUMN_BLOCK_CHARS so the lazy scan can never run to
    # end-of-input on unterminated statements (the unbounded form was
    # quadratic there).
    #
    # Capture groups:
    #   1-4: optional schema prefix (double-quoted / backticked /
    #        single-quoted / bare), each holding the prefix's inner text
    #   5-8: table name (double-quoted / backticked / single-quoted / bare),
    #        each holding the identifier's inner (unquoted) text
    #   9:   the column block
    _QUOTED_IDENTIFIER = r'(?:"([^"]+)"|`([^`]+)`|\'([^\']+)\'|([A-Za-z_][\w$]*))'
    CREATE_TABLE_PATTERN = re.compile(
        r'CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
        r'(?:' + _QUOTED_IDENTIFIER + r'\s*\.\s*)?'
        + _QUOTED_IDENTIFIER
        + r'\s*\(([\s\S]{0,' + str(MAX_COLUMN_BLOCK_CHARS) + r'}?)\)\s*;',
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

    # A $-tag that opens a PostgreSQL dollar-quoted body: $$ or
    # $tag$ where the tag starts with a letter/underscore (pg identifier
    # rules) — a bare "$5" or "$100" is ordinary text, not a tag.
    _DOLLAR_TAG = re.compile(r'\$\$|\$[A-Za-z_][A-Za-z_0-9]*\$')

    @classmethod
    def _decode(cls, data: bytes) -> str:
        """Decode schema-file bytes with a real-world encoding chain.

        Order: an unambiguous BOM selects its codec (a body that fails
        that codec falls through to the legacy chain on the BOM-stripped
        bytes rather than failing the file); otherwise strict UTF-8; on
        failure a lossy cp1252 leg — the encoding Windows tools most
        commonly export — with ``errors='replace'``. Every leg passes
        through :func:`raise_for_suspicious_decode`, so a mis-decode
        (mojibake or BOM-less UTF-16) fails with an accurate error
        instead of indexing garbage. UTF-16 without a BOM is not
        detected (not distinguishable with confidence) and remains out
        of scope.
        """
        for bom, codec in cls._BOM_CODECS:
            if data.startswith(bom):
                try:
                    return cls._validated(data.decode(codec), what="Schema file")
                except UnicodeDecodeError:
                    # The BOM lied about the body (truncated/odd-length
                    # or foreign bytes): retry the plain chain below on
                    # the BOM-stripped bytes instead of failing the file.
                    data = data[len(bom):]
                    break
        try:
            decoded = data.decode('utf-8')
        except UnicodeDecodeError:
            decoded = data.decode('cp1252', errors='replace')
        return cls._validated(decoded, what="Schema file")

    @staticmethod
    def _validated(text: str, *, what: str) -> str:
        raise_for_suspicious_decode(text, what=what)
        return text

    def parse(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Parse a SQL/DDL file into statement chunks.

        Args:
            file_path: Path to the .sql or .ddl file

        Returns:
            List of chunk dictionaries with 'text' and 'metadata' keys
            (``object_type='table'`` chunks for CREATE TABLE blocks,
            ``object_type='other_sql'`` chunks for every other
            top-level statement)

        Raises:
            FileNotFoundError: If the file does not exist
            ValueError: If the file has an invalid extension, exceeds
                the size limit, cannot be decoded accurately, or
                exceeds MAX_STATEMENT_CHUNKS
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
    def _dollar_tag_at(cls, text: str, i: int) -> Optional[str]:
        """Return the dollar-tag opening at ``text[i] == '$'``, if any.

        PostgreSQL dollar quotes look like ``$$`` or ``$tag$``; a lone
        ``$`` (e.g. an operator or placeholder) is ordinary text.
        """
        if i >= len(text) or text[i] != '$':
            return None
        match = cls._DOLLAR_TAG.match(text, i)
        return match.group(0) if match else None

    @classmethod
    def _strip_comments(cls, text: str) -> str:
        """Remove ``--``, ``/* */`` and MySQL ``#`` comments, quote-aware.

        Comment markers inside ANY protected span — '...' strings, "..."
        and `...` quoted identifiers, and PostgreSQL dollar-quoted
        bodies — are content, not comments, and survive verbatim (a
        doubled quote escapes inside its span). Comment markers inside
        comments are consumed with their comment. ``#`` runs to
        end-of-line like ``--`` (MySQL dumps; harmless for dialects
        where ``#`` otherwise appears inside literals, which stay
        protected).
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
            if ch == '$':
                tag = cls._dollar_tag_at(text, i)
                if tag:
                    close = text.find(tag, i + len(tag))
                    if close == -1:
                        i = n
                        continue
                    out.append(text[i:close + len(tag)])
                    i = close + len(tag)
                    continue
            if ch == '-' and i + 1 < n and text[i + 1] == '-':
                newline = text.find('\n', i)
                i = n if newline == -1 else newline
                continue
            if ch == '#':
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
        """Split SQL text on ';' OUTSIDE protected spans.

        Protects '...' / "..." / `...` quoted spans (with doubled-quote
        escapes) and PostgreSQL dollar-quoted bodies, so a semicolon
        inside any literal or function body keeps its statement whole.
        Runs on comment-stripped text, so comment semicolons are
        already gone.
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
            if ch == '$':
                tag = cls._dollar_tag_at(text, i)
                if tag:
                    close = text.find(tag, i + len(tag))
                    body_end = n if close == -1 else close + len(tag)
                    current.append(text[i:body_end])
                    i = body_end
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
        """Extract one chunk per top-level SQL statement.

        Comments are stripped (quote-aware, including MySQL ``#``) once
        up front, the residual is split on ``;`` outside quotes and
        dollar-quoted bodies, and each statement is classified: a
        statement whose (bounded) CREATE TABLE match covers it becomes a
        structured ``table`` chunk — original quoted spelling
        round-trips, metadata carries the bare table name — everything
        else becomes a whitespace-normalized ``other_sql`` chunk. No
        SQL construct is silently dropped (issue #703 / T1-02-K-05), a
        commented-out statement (of any kind) never becomes a chunk,
        and chunks emerge in document order. More than
        MAX_STATEMENT_CHUNKS statements fails the file with an accurate
        error instead of flooding the embedding pipeline.
        """
        chunks: List[Dict[str, Any]] = []

        stripped = self._strip_comments(content)
        for statement in self._split_statements(stripped):
            if not statement.strip():
                continue
            if len(chunks) >= self.MAX_STATEMENT_CHUNKS:
                raise ValueError(
                    f"Schema file contains more than {self.MAX_STATEMENT_CHUNKS} "
                    "SQL statements; split the file or raise "
                    "SchemaParser.MAX_STATEMENT_CHUNKS"
                )
            # The statement carries no trailing ';' (the split consumed
            # it), so probe with the terminator re-attached. Match is
            # anchored to the statement START: a "CREATE TABLE" appearing
            # inside a string literal further into some other statement
            # must not reclassify it (issue #703 review). The bounded
            # column capture keeps this linear per statement even for
            # unterminated input.
            match = self.CREATE_TABLE_PATTERN.match(statement.lstrip() + ';')
            if match is not None:
                bare_name, original_name = self._identifier_parts(match)
                column_block = match.group(9).strip()
                # Reconstruct the full table definition using the
                # original identifier spelling so the definition
                # round-trips.
                table_definition = (
                    f"CREATE TABLE {original_name} (\n{column_block}\n);"
                )
                chunks.append({
                    'text': table_definition,
                    'metadata': {
                        'table_name': bare_name,
                        'object_type': 'table',
                        'source_file': source_file,
                    },
                })
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
