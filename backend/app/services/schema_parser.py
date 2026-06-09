"""
SQL/DDL schema parser for extracting CREATE TABLE definitions.
Parses .sql and .ddl files to extract table schemas as chunks.
"""

import re
from pathlib import Path
from typing import Any, Dict, List


class SchemaParser:
    """
    Parser for SQL/DDL schema files.

    Extracts CREATE TABLE blocks from .sql and .ddl files,
    capturing table names and column definitions.
    """

    # Maximum file size in bytes (100MB)
    MAX_FILE_SIZE = 100 * 1024 * 1024

    # Regex pattern to match CREATE TABLE blocks
    # Handles optional VIRTUAL keyword, IF NOT EXISTS, schema prefix, backticks/quotes
    CREATE_TABLE_PATTERN = re.compile(
        r'CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
        r'(?:[`"\']?(?:\w+)[`"\']?\.)?'
        r'[`"\']?(\w+)[`"\']?\s*\((.*?)\);',
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

        chunks = []

        # Find all CREATE TABLE blocks
        for match in self.CREATE_TABLE_PATTERN.finditer(content):
            table_name = match.group(1)
            column_block = match.group(2).strip()

            # Reconstruct the full table definition
            table_definition = f"CREATE TABLE {table_name} (\n{column_block}\n);"

            chunk = {
                'text': table_definition,
                'metadata': {
                    'table_name': table_name,
                    'object_type': 'table',
                    'source_file': str(path)
                }
            }
            chunks.append(chunk)

        return chunks

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

        chunks = []

        for match in self.CREATE_TABLE_PATTERN.finditer(sql_text):
            table_name = match.group(1)
            column_block = match.group(2).strip()

            table_definition = f"CREATE TABLE {table_name} (\n{column_block}\n);"

            chunk = {
                'text': table_definition,
                'metadata': {
                    'table_name': table_name,
                    'object_type': 'table',
                    'source_file': None
                }
            }
            chunks.append(chunk)

        return chunks
