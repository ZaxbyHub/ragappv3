"""
SQL/DDL schema parser for extracting CREATE TABLE definitions.
Parses .sql and .ddl files to extract table schemas as chunks.
"""

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

        # Read file content with encoding error handling
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            content = path.read_text(encoding='utf-8', errors='replace')

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

    def _extract_chunks(
        self, content: str, source_file: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Extract CREATE TABLE chunks from SQL/DDL content.

        Each emitted chunk's text preserves the ORIGINAL quoted spelling of
        the table identifier (round-trip safe); metadata carries the bare
        (unquoted) identifier under ``table_name``.
        """
        chunks = []

        # Find all CREATE TABLE blocks
        for match in self.CREATE_TABLE_PATTERN.finditer(content):
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

        return chunks
