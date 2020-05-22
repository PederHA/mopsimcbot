import discord

from discord.ext.commands import Bot

from .cogs import SimcCog


bot = Bot(command_prefix=".")


def run(token: str, simc_path: str) -> None:
    bot.add_cog(SimcCog(bot, simc_path))
    bot.run(token)

    