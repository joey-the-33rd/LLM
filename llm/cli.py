import click
from click_default_group import DefaultGroup
import datetime
import json
from llm import Template
from .migrations import migrate
import openai
import os
import pathlib
import shutil
import sqlite_utils
from string import Template as StringTemplate
import sys
import time
import warnings
import yaml

warnings.simplefilter("ignore", ResourceWarning)

DEFAULT_MODEL = "gpt-3.5-turbo"

MODEL_ALIASES = {"4": "gpt-4", "gpt4": "gpt-4", "chatgpt": "gpt-3.5-turbo"}

DEFAULT_TEMPLATE = "prompt: "


@click.group(
    cls=DefaultGroup,
    default="prompt",
    default_if_no_args=True,
)
@click.version_option()
def cli():
    "Access large language models from the command-line"


@cli.command(name="prompt")
@click.argument("prompt", required=False)
@click.option("--system", help="System prompt to use")
@click.option("-m", "--model", help="Model to use")
@click.option("-t", "--template", help="Template to use")
@click.option("--no-stream", is_flag=True, help="Do not stream output")
@click.option("-n", "--no-log", is_flag=True, help="Don't log to database")
@click.option(
    "_continue",
    "-c",
    "--continue",
    is_flag=True,
    flag_value=-1,
    help="Continue the most recent conversation.",
)
@click.option(
    "chat_id",
    "--chat",
    help="Continue the conversation with the given chat ID.",
    type=int,
)
@click.option("--key", help="API key to use")
def prompt(prompt, system, model, template, no_stream, no_log, _continue, chat_id, key):
    "Execute a prompt against on OpenAI model"
    if prompt is None:
        # Read from stdin instead
        prompt = sys.stdin.read()
    openai.api_key = get_key(key, "openai", "OPENAI_API_KEY")
    if template:
        # Cannot be used with system
        if system:
            raise click.ClickException("Cannot use --template and --system together")
        template_obj = load_template(template)
        if not template_obj.prompt:
            # It's a system prompt template
            system = template_obj.system
        else:
            # Interpolate our existing prompt
            input = prompt
            prompt = StringTemplate(template_obj.prompt).substitute(input=input)
            system = template_obj.system
    messages = []
    if _continue:
        _continue = -1
        if chat_id:
            raise click.ClickException("Cannot use --continue and --chat together")
    else:
        _continue = chat_id
    chat_id, history = get_history(_continue)
    history_model = None
    if history:
        for entry in history:
            if entry.get("system"):
                messages.append({"role": "system", "content": entry["system"]})
            messages.append({"role": "user", "content": entry["prompt"]})
            messages.append({"role": "assistant", "content": entry["response"]})
            history_model = entry["model"]
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    if model is None:
        model = history_model or DEFAULT_MODEL
    else:
        # Resolve model aliases
        model = MODEL_ALIASES.get(model, model)
    try:
        debug = {}
        if no_stream:
            start = time.time()
            response = openai.ChatCompletion.create(
                model=model,
                messages=messages,
            )
            debug["model"] = response.model
            debug["usage"] = response.usage
            content = response.choices[0].message.content
            log(no_log, system, prompt, content, model, chat_id, debug, start)
            print(content)
        else:
            start = time.time()
            response = []
            for chunk in openai.ChatCompletion.create(
                model=model,
                messages=messages,
                stream=True,
            ):
                debug["model"] = chunk.model
                content = chunk["choices"][0].get("delta", {}).get("content")
                if content is not None:
                    response.append(content)
                    print(content, end="")
                    sys.stdout.flush()
            print("")
            log(no_log, system, prompt, "".join(response), model, chat_id, debug, start)
    except openai.error.AuthenticationError as ex:
        raise click.ClickException("{}: {}".format(ex.error.type, ex.error.code))
    except openai.error.OpenAIError as ex:
        raise click.ClickException(str(ex))


@cli.command()
def init_db():
    "Ensure log.db SQLite database exists"
    path = log_db_path()
    if path.exists():
        return
    # Ensure directory exists
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite_utils.Database(path)
    db.vacuum()


@cli.group()
def keys():
    "Manage API keys for different models"


@keys.command(name="path")
def keys_path_command():
    "Output path to keys.json file"
    click.echo(keys_path())


def keys_path():
    llm_keys_path = os.environ.get("LLM_KEYS_PATH")
    if llm_keys_path:
        return pathlib.Path(llm_keys_path)
    else:
        return user_dir() / "keys.json"


@keys.command(name="set")
@click.argument("name")
@click.option("--value", prompt="Enter key", hide_input=True, help="Value to set")
def set_(name, value):
    """
    Save a key in keys.json

    Example usage:

    \b
        $ llm keys set openai
        Enter key: ...
    """
    default = {"// Note": "This file stores secret API credentials. Do not share!"}
    path = pathlib.Path(keys_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(default))
    try:
        current = json.loads(path.read_text())
    except json.decoder.JSONDecodeError:
        current = default
    current[name] = value
    path.write_text(json.dumps(current, indent=2) + "\n")


@cli.group(
    cls=DefaultGroup,
    default="list",
    default_if_no_args=True,
)
def logs():
    "Tools for exploring logs"


@logs.command(name="path")
def logs_path():
    "Output path to logs.db file"
    click.echo(log_db_path())


@logs.command(name="list")
@click.option(
    "-n",
    "--count",
    default=3,
    help="Number of entries to show - 0 for all",
)
@click.option(
    "-p",
    "--path",
    type=click.Path(readable=True, exists=True, dir_okay=False),
    help="Path to log database",
)
@click.option("-t", "--truncate", is_flag=True, help="Truncate long strings in output")
def logs_list(count, path, truncate):
    "Show logged prompts and their responses"
    path = pathlib.Path(path or log_db_path())
    if not path.exists():
        raise click.ClickException("No log database found at {}".format(path))
    db = sqlite_utils.Database(path)
    migrate(db)
    rows = list(db["log"].rows_where(order_by="-id", limit=count or None))
    if truncate:
        for row in rows:
            row["prompt"] = _truncate_string(row["prompt"])
            row["response"] = _truncate_string(row["response"])
    click.echo(json.dumps(list(rows), indent=2))


@cli.group()
def templates():
    "Manage prompt templates"


@templates.command(name="list")
def templates_list():
    "List available templates"
    path = template_dir()
    pairs = []
    for file in path.glob("*.yaml"):
        name = file.stem
        template = load_template(name)
        pairs.append((name, template.prompt or ""))
    max_name_len = max(len(p[0]) for p in pairs)
    fmt = "{name:<" + str(max_name_len) + "} : {prompt}"
    for name, prompt in pairs:
        text = fmt.format(name=name, prompt=prompt)
        click.echo(display_truncated(text))


def display_truncated(text):
    console_width = shutil.get_terminal_size()[0]
    if len(text) > console_width:
        return text[: console_width - 3] + "..."
    else:
        return text


@templates.command(name="show")
@click.argument("name")
def templates_show(name):
    "Show the specified template"
    template = load_template(name)
    click.echo(
        yaml.dump(
            dict((k, v) for k, v in template.dict().items() if v is not None),
            indent=4,
            default_flow_style=False,
        )
    )


@templates.command(name="edit")
@click.argument("name")
def templates_edit(name):
    "Edit the specified template"
    # First ensure it exists
    path = template_dir() / f"{name}.yaml"
    if not path.exists():
        path.write_text(DEFAULT_TEMPLATE, "utf-8")
    click.edit(filename=path)
    # Validate that template
    load_template(name)


@templates.command(name="path")
def templates_path():
    "Output path to templates directory"
    click.echo(template_dir())


def template_dir():
    llm_templates_path = os.environ.get("LLM_TEMPLATES_PATH")
    if llm_templates_path:
        path = pathlib.Path(llm_templates_path)
    else:
        path = user_dir() / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _truncate_string(s, max_length=100):
    if len(s) > max_length:
        return s[: max_length - 3] + "..."
    return s


def get_key(key_arg, default_key, env_var=None):
    keys = load_keys()
    if key_arg in keys:
        return keys[key_arg]
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    if key_arg:
        return key_arg
    default = keys.get(default_key)
    if not default:
        message = "No key found - add one using 'llm keys set {}'".format(default_key)
        if env_var:
            message += " or set the {} environment variable".format(env_var)
        raise click.ClickException(message)
    return default


def load_keys():
    path = pathlib.Path(keys_path())
    if path.exists():
        return json.loads(path.read_text())
    else:
        return {}


def user_dir():
    return pathlib.Path(click.get_app_dir("io.datasette.llm"))


def log_db_path():
    llm_log_path = os.environ.get("LLM_LOG_PATH")
    if llm_log_path:
        return pathlib.Path(llm_log_path)
    else:
        return user_dir() / "log.db"


def log(no_log, system, prompt, response, model, chat_id=None, debug=None, start=None):
    duration_ms = None
    if start is not None:
        end = time.time()
        duration_ms = int((end - start) * 1000)
    if no_log:
        return
    log_path = log_db_path()
    if not log_path.exists():
        return
    db = sqlite_utils.Database(log_path)
    migrate(db)
    db["log"].insert(
        {
            "system": system,
            "prompt": prompt,
            "chat_id": chat_id,
            "response": response,
            "model": model,
            "timestamp": str(datetime.datetime.utcnow()),
            "debug": debug,
            "duration_ms": duration_ms,
        },
    )


def load_template(name):
    path = template_dir() / f"{name}.yaml"
    if not path.exists():
        raise click.ClickException(f"Invalid template: {name}")
    try:
        loaded = yaml.safe_load(path.read_text())
    except yaml.YAMLError as ex:
        raise click.ClickException("Invalid YAML: {}".format(str(ex)))
    if isinstance(loaded, str):
        return Template(name=name, prompt=loaded)
    loaded["name"] = name
    return Template.parse_obj(loaded)


def get_history(chat_id):
    if chat_id is None:
        return None, []
    log_path = log_db_path()
    if not log_path.exists():
        raise click.ClickException(
            "This feature requires logging. Run `llm init-db` to create log.db"
        )
    db = sqlite_utils.Database(log_path)
    migrate(db)
    if chat_id == -1:
        # Return the most recent chat
        last_row = list(db["log"].rows_where(order_by="-id", limit=1))
        if last_row:
            chat_id = last_row[0].get("chat_id") or last_row[0].get("id")
        else:  # Database is empty
            return None, []
    rows = db["log"].rows_where(
        "id = ? or chat_id = ?", [chat_id, chat_id], order_by="id"
    )
    return chat_id, rows
