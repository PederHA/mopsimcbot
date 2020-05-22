import asyncio
import subprocess
from pathlib import Path
from functools import partial
from dataclasses import dataclass
from typing import Iterable

import discord
from discord.ext import commands, tasks
from async_timeout import timeout
from pathvalidate import sanitize_filename
from unidecode import unidecode

from ..checks import admins_only
from ..wow import CLASSES


MIN_ITERATIONS = 500
MAX_ITERATIONS = 20000
MIN_THREADS = 1
MAX_THREADS = 4

PARAMS = {
    "html": "out.html",
    "threads": 1,
    "iterations": 5000
}


@dataclass
class SimulationRequest:
    ctx: commands.Context
    character: str
    scaling: bool
    simc_path: Path
    dm: bool = True

    def __post_init__(self) -> None:
        self.event: asyncio.Event = asyncio.Event() # NOTE: remove?
        self.author_name: str = self.ctx.message.author.name # unused
        self.filename: str = sanitize_filename(unidecode(self.author_name))
        self.profile_path: Path = Path(f"profiles/{self.filename}.simc")
    
    def get_character_name(self) -> str:
        """Returns value of class=name from /simc input."""
        for line in self.character.splitlines():
            if any(line.startswith(c) for c in CLASSES):
                return line.split("=")[1].capitalize()
        else:
            return self.author_name # fall back on author name (lazy)
  
    def _get_params(self) -> str:
        return "\n".join(f"{k}={v}" for k, v in PARAMS.items())

    def make_simc_profile(self) -> None:
        # Get 'k=v' string of base parameters
        params = self._get_params()
        if self.scaling:
            params += "\ncalculate_scale_factors=1\n"

        # Make sure directory exists
        if not self.profile_path.parent.exists():
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(self.profile_path, "w", encoding="utf-8") as f:
            f.write(f"{params}\n")
            f.write(self.character)

    async def do_sim(self) -> None:
        self.event.set()
        try:
            self.make_simc_profile()
            to_run = partial(
                subprocess.check_output, 
                f"{self.simc_path} {self.profile_path.resolve()}", 
                stderr=subprocess.STDOUT, 
                shell=True, 
                universal_newlines=True)
            await self.ctx.bot.loop.run_in_executor(None, to_run)
            await self._send_results()
        finally:
            self.event.clear()

    async def _send_results(self) -> None:
        if not self.event.is_set():
            raise AttributeError("Cannot send results before simulation has been completed!")

        if self.dm:
            if not self.ctx.message.author.dm_channel:
                channel = await self.ctx.message.author.create_dm()
            else:
                channel = self.ctx.message.author.dm_channel
            # alt: dm_channel = self.message.author.dm_channel or await self.message.author.create_dm()
        else:
            channel = self.ctx.message.channel
        
        # NOTE: does not support overriding out= in baseprofile.simc yet
        await channel.send(file=discord.File(fp=PARAMS["html"], 
                                                filename=f"{self.filename}.html")) 


class SimcCog(commands.Cog):
    def __init__(self, bot: commands.Bot, simc_path: str) -> None:
        self.bot = bot
        
        self.simc_path = Path(simc_path)
        if not self.simc_path.exists():
            raise FileNotFoundError("Invalid path to SimulationCraft executable")
        
        # Queue variables
        self.queue: Iterable[SimulationRequest] = asyncio.Queue()
        self.current: SimulationRequest = None
        self.queue_loop.start()
        
        self.send_as_dm = False

    @tasks.loop(seconds=2)
    async def queue_loop(self) -> None:
        while not self.queue.empty():
            try:
                await self.get_from_queue()
            except asyncio.TimeoutError:
                pass
            finally:
                self.current = None
    
    async def get_from_queue(self) -> None:
        async with timeout(300):
            try:
                request = await self.queue.get()
                self.current = request
                await request.do_sim()
            except subprocess.CalledProcessError as e:
                await request.ctx.send(f"ERROR: {str(e.stdout)}")
            finally:
                self.current = None

    @commands.command(name="dps")
    async def sim(self, ctx: commands.Context, character: str) -> None:
        """Simulate character dps using output from /simc."""
        await self._add_to_queue(ctx, character, scaling=False)

    @commands.command(name="scaling", aliases=["statweights", "stats"])
    async def sim_scaling(self, ctx: commands.Context, character: str) -> None:
        """Simulate character stat weights using output from /simc."""
        await self._add_to_queue(ctx, character, scaling=True)

    async def _add_to_queue(self, ctx: commands.Context, character: str, scaling: bool) -> None:
        if character.startswith("http"):
            return await ctx.send("Character armory url is not supported (yet)!")
        
        msg = await ctx.send(
            f"Added your character to the queue, **{ctx.message.author.name}**", 
            delete_after=10)

        await self.queue.put(SimulationRequest(ctx,
                                               msg,
                                               character, 
                                               scaling=scaling, 
                                               simc_path=self.simc_path,
                                               dm=self.send_as_dm))

    @commands.command(name="queue")
    async def show_queue(self, ctx: commands.Context) -> None:
        if not self.current and self.queue.empty():
            return await ctx.send("Queue is empty!")
        
        out = ["```"]
        if self.current:
            out.append(f"Currently processing: {self.current.get_character_name()}")
        
        if not self.queue.empty():
            out.append("\nQueued:\n")
            for i, request in enumerate(self.queue._queue, start=1):
                out.append(f"{i}. {request.get_character_name()}")
        
        out.append("```") 
        await ctx.send("\n".join(out))

    @commands.command(name="iterations")
    async def setget_iterations(self, ctx: commands.Context, iterations: int=None) -> None:
        if not iterations:
            return await ctx.send(f"Currently set to {PARAMS['iterations']} iterations.")
        
        if iterations < MIN_ITERATIONS:
            return await ctx.send(f"Number of iterations must be >={MIN_ITERATIONS}!")
        elif iterations > MAX_ITERATIONS:
            return await ctx.send(f"Number of iterations must be <={MAX_ITERATIONS}!")
        else:
            PARAMS["iterations"] = iterations
            await ctx.send(f"Number of iterations set to {iterations}.")

    @commands.command(name="threads")
    @admins_only()
    async def setget_threads(self, ctx: commands.Context, threads: int=None) -> None:
        if not threads:
            return await ctx.send(f"Currently set to {PARAMS['threads']} threads.")
        
        if threads < MIN_THREADS:
            await ctx.send(f"Number of threads must be >={MIN_THREADS}!")
        elif threads > MAX_THREADS:
            await ctx.send(f"Number of threads must be <={MAX_THREADS}!")
        else:
            PARAMS["threads"] = threads
            await ctx.send(f"Number of threads set to {threads}.")

    # TODO make "class Param" with min, max, etc. to avoid these (almost) duplicate methods
    @commands.command(name="settings")
    async def show_settings(self, ctx: commands.Context) -> None:
        skip = ["html"]
        
        out = ["```"]
        for k, v in PARAMS.items():
            if any(k == s for s in skip):
                continue
            out.append(f"{k.capitalize()}: {v}")
        out.append("```")
        await ctx.send("\n".join(out))

    @commands.command(name="addon")
    async def send_simc_addon(self, ctx: commands.Context) -> None:
        addon = Path("files/simulationcraft.zip")
        if not addon.exists():
            return await ctx.send(
                "Unable to send simc addon.\n"
                "Ask bot host to add it under 'files/simulationcraft.zip'"
            )

        await ctx.send(file=discord.File(addon))
