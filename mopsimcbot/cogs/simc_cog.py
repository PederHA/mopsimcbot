import asyncio
import subprocess
from pathlib import Path
from functools import partial
from dataclasses import dataclass
from typing import Iterable, Union, Optional

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


ParamType = Union[str, int]


@dataclass
class Parameter:
    parameter: str
    value: ParamType
    v_min: Optional[int] = None
    v_max: Optional[int] = None

    def set_value(self, value: ParamType) -> None:
        if type(value) != type(self.value):
            raise TypeError(f"Type mismatch. Parameter value must be type: {type(self.value)}")
        
        if isinstance(value, str):
            self.value = value
        elif isinstance(value, int):
            if (self.v_min is not None and value < self.v_min):
                raise ValueError(f"{self.parameter} value must be at least {self.v_min}.")
            elif (self.v_max is not None and value > self.v_max):
                raise ValueError(f"{self.parameter} value cannot exceed {self.v_min}.")
            self.value = value
        else:
            raise TypeError(
                f"Invalid value type. Must be one of "
                f"{Parameter.__annotations__['value'].__args__}"
            )

    def __str__(self) -> str:
        return f"{self.parameter}={self.value}"


PARAMS = {
    "html": Parameter("html", "out.html"),
    "threads": Parameter("threads", 1, v_min=1, v_max=4),
    "iterations": Parameter("iterations", 5000, v_min=500, v_max=20000),
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
        self.filename: str = sanitize_filename(unidecode(self.get_character_name()))
        self.profile_path: Path = Path(f"profiles/{self.filename}.simc")
    
    def get_character_name(self) -> str:
        """Returns value of class=name from /simc input."""
        for line in self.character.splitlines():
            if any(line.startswith(c) for c in CLASSES):
                return line.split("=")[1].capitalize()
        else:
            return self.author_name # fall back on author name (should raise exception)
  
    def _get_params(self) -> str:
        return "\n".join(str(p) for p in PARAMS.values())

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
        self.event.set() # Improper use of asyncio.Event. Should just do away with it.
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
        
        await channel.send(
            f"Your simulation results are ready, {self.ctx.message.author.mention}.",
            file=discord.File(fp=PARAMS["html"].value, 
                              filename=f"{self.filename}.html")
        ) 


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
    
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        print("Bot logged in")
    
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
        
        request = SimulationRequest(ctx,
                                    character, 
                                    scaling=scaling, 
                                    simc_path=self.simc_path,
                                    dm=self.send_as_dm)

        await ctx.send(
            f"Added **`{request.get_character_name()}`** to the queue.", 
            delete_after=10)

        await self.queue.put(request)

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
            return await ctx.send(f"Currently set to {PARAMS['iterations'].value} iterations.")
        await self._set_param(ctx, "iterations", iterations)

    @commands.command(name="threads")
    @admins_only()
    async def setget_threads(self, ctx: commands.Context, threads: int=None) -> None:
        if not threads:
            return await ctx.send(f"Currently set to {PARAMS['threads'].value} threads.")
        await self._set_param(ctx, "threads", threads)

    async def _set_param(self, ctx: commands.Context, param: str, value: ParamType) -> None:
        if param not in PARAMS:
            raise KeyError(f"No parameter named '{param}'!")  
        try:
            PARAMS[param].set_value(value)
        except Exception as e:
            return await ctx.send("\n".join(e.args))
        else:
            await ctx.send(f"`{param.capitalize()}` set to {value}.")
       
    @commands.command(name="settings")
    async def show_settings(self, ctx: commands.Context) -> None:
        skip = ["html"]
        
        out = ["```"]
        for p in PARAMS.values():
            if any(p.parameter == s for s in skip):
                continue
            out.append(f"{p.parameter.capitalize()}: {p.value}")
        out.append("```")
        await ctx.send("\n".join(out))

    @commands.command(name="addon")
    async def send_simc_addon(self, ctx: commands.Context) -> None:
        addon = Path("files/simulationcraft.zip")
        if not addon.exists():
            return await ctx.send(
                "Unable to send simc addon.\n"
                "Ask bot owner to add it under 'files/simulationcraft.zip'"
            )

        await ctx.send(file=discord.File(addon))
