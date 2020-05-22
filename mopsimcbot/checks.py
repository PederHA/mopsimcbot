
from discord.ext import commands

OWNER_ID = 103890994440728576 # (you)

# Decorator check
def admins_only():
    def predicate(ctx):
        if hasattr(ctx.author, "guild_permissions"): # Disables privileged commands in PMs
            return ctx.author.guild_permissions.administrator or ctx.author.id == OWNER_ID
        return False
    return commands.check(predicate)
