import base64
import logging
import os
import re
import traceback
from dataclasses import dataclass
from dataclasses import field as datafield
from difflib import get_close_matches
from enum import Enum
from pathlib import Path
from typing import Any

import discord
import requests
from dateutil.parser import parse as parse_date
from discord.ext import commands

dir_type = "ballsdex" if os.path.isdir("ballsdex") else "carfigures"

if dir_type == "ballsdex":
    from ballsdex.core.models import Ball, Economy, GuildConfig, Regime, Special
    from ballsdex.settings import settings
else:
    from carfigures.core.models import Car as Ball
    from carfigures.core.models import CarType as Regime
    from carfigures.core.models import Country as Economy
    from carfigures.core.models import Event as Special
    from carfigures.core.models import GuildConfig
    from carfigures.settings import settings


log = logging.getLogger(f"{dir_type}.core.dexscript")

__version__ = "0.4.3.2"


START_CODE_BLOCK_RE = re.compile(r"^((```sql?)(?=\s)|(```))")
FILENAME_RE = re.compile(r"^(.+)(\.\S+)$")

MODELS = {
    "guildconfig": [GuildConfig, "ID"],
    "ball": [Ball, "COUNTRY"],
    "regime": [Regime, "NAME"],
    "economy": [Economy, "NAME"],
    "special": [Special, "NAME"],
}

SETTINGS = {
    "DEBUG": False,
    "OUTDATED-WARNING": True,
    "REFERENCE": "main",
}

dex_yields = []


class Types(Enum):
    METHOD = 0
    NUMBER = 1
    STRING = 2
    BOOLEAN = 3
    MODEL = 4
    DATETIME = 5


class YieldType(Enum):
    CREATE_MODEL = 0


class CodeStatus(Enum):
    SUCCESS = 0
    FAILURE = 1


class DexScriptError(Exception):
    pass


# Ported from the Ballsdex admin package.
async def save_file(attachment: discord.Attachment) -> Path:
    path = Path(f"./static/uploads/{attachment.filename}")
    match = FILENAME_RE.match(attachment.filename)

    if not match:
        raise TypeError("The file you uploaded lacks an extension.")
    
    i = 1

    while path.exists():
        path = Path(f"./static/uploads/{match.group(1)}-{i}{match.group(2)}")
        i = i + 1
    
    await attachment.save(path)
    return path

def in_list(list_attempt, index):
    try:
        list_attempt[index]
        return True
    except Exception:
        return False


@dataclass
class Value:
    name: Any
    type: Types
    extra_data: list = datafield(default_factory=list)

    def __str__(self):
        return str(self.name)


@dataclass
class Yield:
    model: Any
    identifier: Any
    value: dict
    type: YieldType

    @staticmethod
    def get(model, identifier):
        return next(
            (x for x in dex_yields if (x.model, x.identifier.name) == (model, identifier)), None
        )


class Methods:
    def __init__(self, parser, ctx, args: list[Value]):
        self.ctx = ctx
        self.args = args

        self.parser = parser

    async def push(self):
        global dex_yields

        if in_list(self.args, 1) and self.args[1].name.lower() == "-clear":
            dex_yields = []

            await self.ctx.send("Cleared yield cache.")
            return

        for index, yield_object in enumerate(dex_yields, start=1):
            if in_list(self.args, 1) and int(self.args[1].name) >= index:
                break

            match yield_object.type:
                case YieldType.CREATE_MODEL:
                    await yield_object.model.create(**yield_object.value)

        plural = "" if len(dex_yields) == 1 else "s"
        number = self.args[1].name if in_list(self.args, 1) else len(dex_yields)

        await self.ctx.send(f"Pushed `{number}` yield{plural}.")

        dex_yields = []

    async def create(self):
        result = await self.parser.create_model(
            self.args[1].name, self.args[2], self.args[3] if in_list(self.args, 3) else False
        )

        suffix = ""

        if result is not None:
            suffix = " and yielded it until `push`"
            dex_yields.append(result)

        await self.ctx.send(f"Created `{self.args[2]}`{suffix}")

    async def delete(self):
        returned_model = await self.parser.get_model(self.args[1], self.args[2].name)

        await returned_model.delete()

        await self.ctx.send(f"Deleted `{self.args[2]}`")

    async def update(self):
        found_yield = Yield.get(self.args[1].name, self.args[2].name)

        new_attribute = None

        if self.ctx.message.attachments != []:
            image_path = await save_file(self.ctx.message.attachments[0])
            new_attribute = Value(f"/{image_path}", Types.STRING)
        else:
            new_attribute = self.args[4]

        update_message = f"`{self.args[2]}'s` {self.args[3]} to `{new_attribute.name}`"

        if found_yield is None:
            returned_model = await self.parser.get_model(self.args[1], self.args[2].name)

            setattr(returned_model, self.args[3].name.lower(), new_attribute.name)

            await returned_model.save()

            await self.ctx.send(f"Updated {update_message}")

            return

        found_yield.value[self.args[3].name.lower()] = new_attribute.name

        await self.ctx.send(f"Updated yielded {update_message}")

    async def view(self):
        returned_model = await self.parser.get_model(self.args[1], self.args[2].name)

        if not in_list(self.args, 3):
            fields = {"content": "```"}

            for key, value in vars(returned_model).items():
                if key.startswith("_"):
                    continue

                fields["content"] += f"{key}: {value}\n"

                if isinstance(value, str) and value.startswith("/static"):
                    if fields.get("files") is None:
                        fields["files"] = []
                    fields["files"].append(discord.File(value[1:]))

            fields["content"] += "```"

            await self.ctx.send(**fields)
            return

        attribute = getattr(returned_model, self.args[3].name.lower())

        if isinstance(attribute, str) and os.path.isfile(attribute[1:]):
            await self.ctx.send(f"```{attribute}```", file=discord.File(attribute[1:]))
            return

        await self.ctx.send(f"```{attribute}```")

    async def list(self):
        model = self.args[1].name

        parameters = "GLOBAL YIELDS:\n\n"

        model_name = model if isinstance(model, str) else model.__name__

        if model_name.lower() != "-yields":
            parameters = f"{model_name.upper()} FIELDS:\n\n"

            for field in vars(model()):  # type: ignore
                if field[:1] == "_":
                    continue

                parameters += f"- {field.replace(' ', '_').upper()}\n"
        else:
            for index, dex_yield in enumerate(dex_yields, start=1):
                parameters += f"{index}. {dex_yield.identifier.name.upper()}\n"

        await self.ctx.send(f"```\n{parameters}\n```")

    async def file(self):
        match self.args[1].name.lower():
            case "write":
                new_file = self.ctx.message.attachments[0]

                with open(self.args[2].name, "w") as opened_file:
                    contents = await new_file.read()
                    opened_file.write(contents.decode("utf-8"))

                await self.ctx.send(f"Wrote to `{self.args[2]}`")

            case "clear":
                with open(self.args[2].name, "w") as _:
                    pass

                await self.ctx.send(f"Cleared `{self.args[2]}`")

            case "read":
                await self.ctx.send(file=discord.File(self.args[2].name))

            case "delete":
                os.remove(self.args[1].name)

                await self.ctx.send(f"Deleted `{self.args[1]}`")

            case _:
                raise DexScriptError(
                    f"'{self.args[0]}' is not a valid file operation. "
                    "(READ, WRITE, CLEAR, or DELETE)"
                )

    async def show(self):
        await self.ctx.send(f"```\n{self.args[1]}\n```")


class DexScriptParser:
    """
    This class is used to parse DexScript into Python code.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.values = []

    @staticmethod
    def is_number(string):
        try:
            float(string)
            return True
        except ValueError:
            return False

    @staticmethod
    def is_date(string):
        try:
            parse_date(string)
            return True
        except Exception:
            return False

    @staticmethod
    def autocorrect(string, correction_list, error="does not exist."):
        autocorrection = get_close_matches(string, correction_list)

        if not autocorrection or autocorrection[0] != string:
            suggestion = f"\nDid you mean '{autocorrection[0]}'?" if autocorrection else ""

            raise DexScriptError(f"'{string}' {error}{suggestion}")

        return autocorrection[0]

    async def create_model(self, model, identifier, yield_creation):
        fields = {}

        for key, field in vars(model()).items():
            if field is not None:
                continue

            if key in ["id", "short_name"]:
                continue

            fields[key] = 1

            if key in ["country", "full_name", "catch_names", "name"]:
                fields[key] = identifier
            elif key == "emoji_id":
                fields[key] = 100**8
            elif key == "regime_id":
                first_regime = await Regime.first()
                fields[key] = first_regime.pk

        if yield_creation:
            return Yield(model, identifier, fields, YieldType.CREATE_MODEL)

        await model.create(**fields)

    async def get_model(self, model, identifier):
        try:
            returned_model = await model.name.filter(
                **{
                    self.translate(model.extra_data[0].lower()): self.autocorrect(
                        identifier, [str(x) for x in await model.name.all()]
                    )
                }
            )
        except AttributeError:
            raise DexScriptError(f"{model} is not a valid model.")

        return returned_model[0]

    def var(self, value):
        return_value = value

        match value.type:
            case Types.MODEL:
                current_model = MODELS[value.name.lower()]

                value.name = current_model[0]
                value.extra_data.append(current_model[1])

            case Types.BOOLEAN:
                value.name = value.name.lower() == "true"

            case Types.DATETIME:
                value.name = parse_date(value.name)

        return return_value

    @staticmethod
    def translate(string: str, item=None):
        """
        Translates model and field names into a format for both Ballsdex and CarFigures.

        Parameters
        ----------
        string: str
          The string you want to translate.
        """

        if dir_type == "ballsdex":
            return getattr(item, string) if item else string

        translation = {"BALL": "ENTITY", "COUNTRY": "full_name"}

        translated_string = translation.get(string.upper(), string)

        return getattr(item, translated_string) if item else translated_string

    def create_value(self, line):
        type = Types.STRING

        value = Value(line, type)
        lower = line.lower()

        if lower in vars(Methods):
            type = Types.METHOD
        elif lower in MODELS:
            type = Types.MODEL
        elif self.is_date(lower) and lower.count("-") >= 2:
            type = Types.DATETIME
        elif self.is_number(lower):
            type = Types.NUMBER
        elif lower in ["true", "false"]:
            type = Types.BOOLEAN

        value.type = type

        return self.var(value)

    async def execute(self, code: str):
        try:
            seperator = "\n" if "\n" in code else ";'"

            split_code = [x for x in code.split(seperator) if x.strip() != ""]

            parsed_code: list[list[Value]] = []

            for line in split_code:
                line_code: list[Value] = []
                full_line = ""

                for index2, char in enumerate(line):
                    if char == "":
                        continue

                    full_line += char

                    if full_line == "--":
                        break

                    if char in [">"] or index2 == len(line) - 1:
                        line_code.append(self.create_value(full_line.replace(">", "").strip()))

                        full_line = ""

                        if len(line_code) == len(line.split(">")):
                            parsed_code.append(line_code)

            for line2 in parsed_code:
                for value in line2:
                    if value.type != Types.METHOD:
                        continue

                    new_method = Methods(self, self.ctx, line2)

                    try:
                        await getattr(new_method, value.name.lower())()
                    except IndexError:
                        # TODO: Remove `error` duplicates.

                        error = f"Argument is missing when calling {value.name}."
                        return ((error, error), CodeStatus.FAILURE)
        except Exception as error:
            return ((error, traceback.format_exc()), CodeStatus.FAILURE)

        return (None, CodeStatus.SUCCESS)


class DexScript(commands.Cog):
    """
    DexScript commands
    """

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def cleanup_code(content):
        """
        Automatically removes code blocks from the code.
        """

        if content.startswith("```") and content.endswith("```"):
            return START_CODE_BLOCK_RE.sub("", content)[:-3]

        return content.strip("` \n")

    @staticmethod
    def check_version():
        if not SETTINGS["OUTDATED-WARNING"]:
            return None

        r = requests.get(
            "https://api.github.com/repos/Dotsian/DexScript/contents/version.txt",
            {"ref": SETTINGS["REFERENCE"]},
        )

        if r.status_code != requests.codes.ok:
            return

        new_version = base64.b64decode(r.json()["content"]).decode("UTF-8").rstrip()

        if new_version != __version__:
            return (
                f"Your DexScript version ({__version__}) is outdated. "
                f"Please update to version ({new_version}) "
                f"using `{settings.prefix}update-ds`."
            )

        return None

    @commands.command()
    @commands.is_owner()
    async def run(self, ctx: commands.Context, *, code: str):
        """
        Executes DexScript code.

        Parameters
        ----------
        code: str
          The code you'd like to execute.
        """

        body = self.cleanup_code(code)

        version_check = self.check_version()

        if version_check:
            await ctx.send(f"-# {version_check}")

        dexscript_instance = DexScriptParser(ctx)
        result, status = await dexscript_instance.execute(body)

        if status == CodeStatus.FAILURE and result is not None:
            await ctx.send(f"```ERROR: {result[SETTINGS['DEBUG']]}\n```")
        else:
            await ctx.message.add_reaction("✅")

    @commands.command()
    @commands.is_owner()
    async def about(self, ctx: commands.Context):
        """
        Displays information about DexScript.
        """

        embed = discord.Embed(
            title="DexScript - BETA",
            description=(
                "DexScript is a set of commands created by DotZZ "
                "that allows you to easily "
                "modify, delete, and display data for models.\n\n"
                "For a guide on how to use DexScript, "
                "refer to the official [DexScript guide](<https://github.com/Dotsian/DexScript/wiki/Commands>).\n\n"
                "If you want to follow DexScript, "
                "join the official [DexScript Discord](<https://discord.gg/EhCxuNQfzt>) server."
            ),
            color=discord.Color.from_str("#03BAFC"),
        )

        version_check = "OUTDATED" if self.check_version() is not None else "LATEST"

        embed.set_thumbnail(url="https://i.imgur.com/uKfx0qO.png")
        embed.set_footer(text=f"DexScript {__version__} ({version_check})")

        await ctx.send(embed=embed)

    @commands.command(name="update-ds")
    @commands.is_owner()
    async def update_ds(self, ctx: commands.Context):
        """
        Updates DexScript to the latest version.
        """

        r = requests.get(
            "https://api.github.com/repos/Dotsian/DexScript/contents/installer.py",
            {"ref": SETTINGS["REFERENCE"]},
        )

        if r.status_code == requests.codes.ok:
            content = base64.b64decode(r.json()["content"])
            await ctx.invoke(self.bot.get_command("eval"), body=content.decode("UTF-8"))
        else:
            await ctx.send(
                "Failed to update DexScript. Report this issue to `dot_zz` on Discord.\n"
                f"```\nERROR CODE: {r.status_code}\n```"
            )

    @commands.command(name="reload-ds")
    @commands.is_owner()
    async def reload_ds(self, ctx: commands.Context):
        """
        Reloads DexScript.
        """

        await self.bot.reload_extension(f"{dir_type}.core.dexscript")
        await ctx.send("Reloaded DexScript")

    @commands.command()
    @commands.is_owner()
    async def setting(self, ctx: commands.Context, setting: str, value: str):
        """
        Changes a setting based on the value provided.

        Parameters
        ----------
        setting: str
          The setting you want to toggle.
        value: str
          The value you want to set the setting to.
        """

        response = f"`{setting}` is not a valid setting."

        if setting in SETTINGS:
            selected_setting = SETTINGS[setting]

            if isinstance(selected_setting, bool):
                SETTINGS[setting] = bool(value)
            elif isinstance(selected_setting, str):
                SETTINGS[setting] = value

            response = f"`{setting}` has been set to `{value}`"

        await ctx.send(response)


async def setup(bot):
    await bot.add_cog(DexScript(bot))
