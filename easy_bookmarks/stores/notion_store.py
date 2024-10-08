import datetime
import logging
import time
from itertools import islice
from typing import Any

import polars as pl
from notion_client import Client
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class NotionConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="notion_", case_sensitive=False)

    token: SecretStr
    base_parent_id: str


class NotionStore(BaseModel):
    config: NotionConfig
    table_name: str
    key_field: str

    database_id: str | None = None

    _notion: Client

    def __init__(self, **data):
        super().__init__(**data)
        self._notion = Client(auth=self.config.token.get_secret_value())

    def _polars_to_notion_type(self, polars_type: pl.DataType) -> str:
        """Map Polars types to Notion property types."""
        type_mapping = {
            pl.String: "rich_text",
            pl.Int64: "number",
            pl.Float64: "number",
            pl.Boolean: "checkbox",
            pl.Date: "date",
            pl.Time: "rich_text",
            pl.Datetime: "date",
            pl.List: "multi_select",
            pl.Struct: "rich_text",
            pl.Object: "rich_text",
        }
        if polars_type not in type_mapping.keys():
            default_type = "rich_text"
            for key in list(type_mapping.keys()):
                if isinstance(polars_type, key):
                    return type_mapping[key]
            return default_type

        return type_mapping[polars_type]

    def create_table(self, fields: dict[str, str]) -> None:
        """
        Create a new Notion database with the specified fields.

        Args:
            table_name (str): The name of the table (database) to create.
            fields (Dict[str, str]): A dictionary of field names and their Polars types.
        """
        properties = {}
        for field_name, field_type in fields.items():
            notion_type = self._polars_to_notion_type(field_type)
            if field_name == self.key_field:
                properties[field_name] = {"title": {}}
            else:
                properties[field_name] = {notion_type: {}}

        response: dict[str, Any] = self._notion.databases.create(
            parent={"type": "page_id", "page_id": self.config.base_parent_id},
            title=[{"type": "text", "text": {"content": self.table_name}}],
            properties=properties,
        )

        self.database_id = response.get("id")

        log.info(f"Database {self.table_name} created.")

        return response.get("id")

    def add_bookmark(self, bookmark_data: dict[str, Any]) -> str:
        MAX_NOTION_FIELD_LENGTH = 2000 - 200

        if not self.database_id:
            raise ValueError(
                "Database ID not set. For creating new table from records use create_table or add_bookmark_from_df methods."
            )

        properties = {}
        for field_name, field_value in bookmark_data.items():
            if field_name == self.key_field:
                properties[field_name] = {
                    "title": [
                        {
                            "text": {
                                "content": str(field_value[0:MAX_NOTION_FIELD_LENGTH])
                            }
                        }
                    ]
                }
            elif isinstance(field_value, (int, float)):
                properties[field_name] = {"number": field_value}
            elif isinstance(field_value, bool):
                properties[field_name] = {"checkbox": field_value}
            elif isinstance(field_value, (datetime.date, datetime.datetime)):
                properties[field_name] = {"date": {"start": field_value.isoformat()}}
            else:
                properties[field_name] = {
                    "rich_text": [
                        {
                            "text": {
                                "content": str(field_value[0:MAX_NOTION_FIELD_LENGTH])
                            }
                        }
                    ]
                }

        response = self._notion.pages.create(
            parent={"database_id": self.database_id}, properties=properties
        )
        return response.get("id")

    def add_bookmarks_from_df(
        self, df: pl.DataFrame, batch_size: int = 5, time_sleep: int = 2
    ) -> None:
        if self.database_id is None:
            log.warning("Database ID not set. Creating a new table.")
            self.create_table(dict(zip(df.columns, df.dtypes)))

        rows = df.iter_rows(named=True)

        while batch := list(islice(rows, batch_size)):
            for row in batch:
                self.add_bookmark(row)

            time.sleep(time_sleep)

    def get_bookmarks_df(self) -> pl.DataFrame:
        response = self._notion.databases.query(database_id=self.database_id)
        response_results = response.get("results")

        data = []
        for item in response_results:
            row = {}
            for k, v in item["properties"].items():
                if "rich_text" in v and v["rich_text"]:
                    row[k] = v["rich_text"][0]["text"]["content"]
                elif "date" in v and v["date"]:
                    row[k] = v["date"]["start"]
                elif "title" in v and v["title"]:
                    row[k] = v["title"][0]["text"]["content"]
                elif "number" in v:
                    row[k] = v["number"]
                elif "checkbox" in v:
                    row[k] = v["checkbox"]
                else:
                    row[k] = "Unknown type"
            data.append(row)

        return pl.DataFrame(data)
