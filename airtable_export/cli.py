import click
import httpx
from httpx import HTTPError
import json as json_
import pathlib
from urllib.parse import quote, urlencode
import sqlite_utils
import time
import yaml as yaml_


@click.command()
@click.version_option()
@click.argument(
    "output_path",
    type=click.Path(file_okay=False, dir_okay=True, allow_dash=False),
    required=True,
)
@click.argument(
    "base_id",
    type=str,
    required=True,
)
@click.argument("tables", type=str, nargs=-1)
@click.option("--key", envvar="AIRTABLE_KEY", help="Airtable API key", required=True)
@click.option(
    "--http-read-timeout",
    help="Timeout (in seconds) for network read operations",
    type=int,
)
@click.option("--user-agent", help="User agent to use for requests")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.option("--json", is_flag=True, help="JSON format")
@click.option("--ndjson", is_flag=True, help="Newline delimited JSON format")
@click.option("--yaml", is_flag=True, help="YAML format (default)")
@click.option(
    "--sqlite",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=False),
    help="Export to this SQLite database",
)
@click.option(
    "--schema",
    is_flag=True,
    help="Save Airtable schema to output_path/_schema.json",
)
@click.option(
    "--download-attachments",
    is_flag=True,
    help="Download attachments and save them to disk",
)
def cli(
    output_path,
    base_id,
    tables,
    key,
    http_read_timeout,
    user_agent,
    verbose,
    json,
    ndjson,
    yaml,
    sqlite,
    schema,
    download_attachments,
):
    "Export Airtable data to YAML file on disk"
    output = pathlib.Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    if not json and not ndjson and not yaml and not sqlite:
        yaml = True
    write_batch = lambda table, batch: None
    if sqlite:
        db = sqlite_utils.Database(sqlite)
        write_batch = lambda table, batch: db[table].insert_all(
            batch, pk="airtable_id", replace=True, alter=True
        )
    if not tables or schema:
        # Fetch all tables
        schema_data = list_tables(base_id, key, user_agent=user_agent)
        dumped_schema = json_.dumps(schema_data, sort_keys=True, indent=4)
        (output / "_schema.json").write_text(dumped_schema, "utf-8")
        if not tables:
            tables = [table["name"] for table in schema_data["tables"]]

    for table in tables:
        records = []
        try:
            db_batch = []
            for record in all_records(
                base_id, table, key, http_read_timeout, user_agent=user_agent
            ):
                r = {
                    **{"airtable_id": record["id"]},
                    **record["fields"],
                    **{"airtable_createdTime": record["createdTime"]},
                }
                records.append(r)
                db_batch.append(r)
                if len(db_batch) == 100:
                    write_batch(table, db_batch)
                    db_batch = []
        except HTTPError as exc:
            raise click.ClickException(exc)
        write_batch(table, db_batch)
        filenames = []
        if json:
            filename = "{}.json".format(table)
            dumped = json_.dumps(records, sort_keys=True, indent=4)
            (output / filename).write_text(dumped, "utf-8")
            filenames.append(output / filename)
        if ndjson:
            filename = "{}.ndjson".format(table)
            dumped = "\n".join(json_.dumps(r, sort_keys=True) for r in records)
            (output / filename).write_text(dumped, "utf-8")
            filenames.append(output / filename)
        if yaml:
            filename = "{}.yml".format(table)
            dumped = yaml_.dump(records, sort_keys=True)
            (output / filename).write_text(dumped, "utf-8")
            filenames.append(output / filename)
        if verbose:
            click.echo(
                "Wrote {} record{} to {}".format(
                    len(records),
                    "" if len(records) == 1 else "s",
                    ", ".join(map(str, filenames)),
                ),
                err=True,
            )
        if download_attachments:
            if verbose:
                click.echo(
                    "\tChecking for attachments to download...",
                    err=True,
                )
            for record in records:
                for field, cell_value in record.items():
                    # If the cell value is a list and the first item is a
                    # dictionary with a key "url", we assume it's an attachment field
                    if isinstance(cell_value, list) and "url" in cell_value[0]:
                        for attachment in cell_value:
                            response = httpx.get(attachment["url"])
                            response.raise_for_status()
                            file_destination = (
                                output
                                / "attachments/{}/{}/{}__{}".format(
                                    table,
                                    record["airtable_id"],
                                    attachment["id"],
                                    attachment["filename"],
                                )
                            )
                            file_destination.parent.mkdir(parents=True, exist_ok=True)
                            file_destination.write_bytes(response.content)
                            if verbose:
                                click.echo(
                                    "\t\tDownloaded attachment to '{}'".format(
                                        file_destination
                                    ),
                                    err=True,
                                )


def list_tables(base_id, api_key, user_agent=None):
    url = f"https://api.airtable.com/v0/meta/bases/{base_id}/tables"
    headers = {"Authorization": "Bearer {}".format(api_key)}
    if user_agent is not None:
        headers["user-agent"] = user_agent
    return httpx.get(url, headers=headers).json()


def all_records(base_id, table, api_key, http_read_timeout, sleep=0.2, user_agent=None):
    headers = {"Authorization": "Bearer {}".format(api_key)}
    if user_agent is not None:
        headers["user-agent"] = user_agent

    if http_read_timeout:
        timeout = httpx.Timeout(5, read=http_read_timeout)
        client = httpx.Client(timeout=timeout)
    else:
        client = httpx

    first = True
    offset = None
    while first or offset:
        first = False
        url = "https://api.airtable.com/v0/{}/{}".format(base_id, quote(table, safe=""))
        if offset:
            url += "?" + urlencode({"offset": offset})
        response = client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        offset = data.get("offset")
        yield from data["records"]
        if offset and sleep:
            time.sleep(sleep)


def str_representer(dumper, data):
    try:
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    except TypeError:
        pass
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml_.add_representer(str, str_representer)
