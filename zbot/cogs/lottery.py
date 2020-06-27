import datetime
import random
import typing

import discord
from discord.ext import commands

from zbot import checker
from zbot import converter
from zbot import database
from zbot import error_handler
from zbot import exceptions
from zbot import logger
from zbot import scheduler
from zbot import utils
from zbot import zbot
from . import _command


class Lottery(_command.Command):

    """Commands for management of lotteries."""

    DISPLAY_NAME = "Loteries & Tirages au sort"
    DISPLAY_SEQUENCE = 5
    MOD_ROLE_NAMES = ['Administrateur', 'Modérateur', 'Annonceur']
    USER_ROLE_NAMES = ['Joueur']

    ANNOUNCE_ROLE_NAME = 'Abonné Annonces'
    EMBED_COLOR = 0x77B255  # Four leaf clover green
    JOBSTORE = database.MongoDBConnector.PENDING_LOTTERIES_COLLECTION

    pending_lotteries = {}  # Local cache of lottery data for reactions check

    def __init__(self, bot):
        super().__init__(bot)
        # Use class attribute to be available from static methods
        Lottery.pending_lotteries = zbot.db.load_pending_jobs_data(
            self.JOBSTORE,
            data_keys=(
                '_id', 'lottery_id', 'message_id', 'channel_id', 'emoji_code', 'nb_winners',
                'next_run_time', 'organizer_id'
            )
        )

    @commands.group(
        name='lottery',
        brief="Gère les tirages au sort",
        invoke_without_command=True
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def lottery(self, context):
        if context.invoked_subcommand is None:
            raise exceptions.MissingSubCommand(context.command.name)

    @lottery.command(
        name='setup',
        aliases=['s', 'set', 'plan'],
        usage="<\"announce\"> <#dest_channel> <:emoji:> <nb_winners> <\"time\"> [--no-announce]",
        brief="Programme un tirage au sort",
        help="Le bot publie une **annonce** dans le **canal de destination**. Les joueurs "
             "participent en cliquant sur l'**émoji** de réaction. À la **date et heure** "
             "indiquées (au format `\"YYYY-MM-DD HH:MM:SS\"`), un **nombre de gagnants** sont "
             "tirés au sort et contactés par MP par le bot. L'organisateur reçoit par MP une copie "
             "du résultat et de la liste des participants injoignables. Par défaut, l'annonce "
             "mentionne automatiquement le rôle `@Abonné Annonces`. Pour éviter cela, il faut "
             "ajouter l'argument `--no-announce`.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_mod_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def setup(
            self, context: commands.Context,
            announce: str,
            dest_channel: discord.TextChannel,
            emoji: converter.to_emoji,
            nb_winners: converter.to_positive_int,
            time: converter.to_future_datetime,
            *, options=""
    ):
        # Check arguments
        if not context.author.permissions_in(dest_channel).send_messages:
            raise exceptions.ForbiddenChannel(dest_channel)

        # Run command
        organizer = context.author
        do_announce = not utils.is_option_enabled(options, 'no-announce')
        prefixed_announce = utils.make_announce(
            context.guild, announce, do_announce and self.ANNOUNCE_ROLE_NAME
        )
        embed = self.build_announce_embed(emoji, nb_winners, organizer, time, self.guild.roles)
        message = await dest_channel.send(prefixed_announce, embed=embed)
        await message.add_reaction(emoji)

        # Register data
        job_id = scheduler.schedule_stored_job(self.JOBSTORE, time, self.run_lottery, message.id).id
        lottery_data = {
            'lottery_id': self.get_next_lottery_id(),
            'message_id': message.id,
            'channel_id': dest_channel.id,
            'emoji_code': emoji if isinstance(emoji, str) else emoji.id,
            'nb_winners': nb_winners,
            'organizer_id': organizer.id,
        }
        zbot.db.update_job_data(self.JOBSTORE, job_id, lottery_data)
        # Add data managed by scheduler later to avoid updating the database with them
        lottery_data.update({'_id': job_id, 'next_run_time': converter.to_timestamp(time)})
        self.pending_lotteries[message.id] = lottery_data

        # Confirm command
        await context.send(
            f"Tirage au sort d'identifiant `{lottery_data['lottery_id']}` programmé : <{message.jump_url}>."
        )

    @staticmethod
    def build_announce_embed(emoji, nb_winners, organizer, time, guild_roles):
        embed = discord.Embed(
            title=f"Tirage au sort le {converter.humanize_datetime(time)} :alarm_clock:",
            color=Lottery.EMBED_COLOR
        )
        embed.add_field(
            name="Nombre de gagnants",
            value=f"**{nb_winners}** joueur{('s' if nb_winners > 1 else '')}"
        )
        embed.add_field(
            name="Inscription",
            value=f"Réagissez avec {emoji}"
        )
        embed.add_field(
            name="Rôle requis",
            value=utils.try_get(
                guild_roles,
                error=exceptions.UnknownRole(Lottery.USER_ROLE_NAMES[0]),
                name=Lottery.USER_ROLE_NAMES[0]
            ).mention
        )
        embed.set_author(
            name=f"Organisateur : @{organizer.display_name}",
            icon_url=organizer.avatar_url
        )
        return embed

    def get_next_lottery_id(self) -> int:
        pending_lottery_ids = [lottery_data['lottery_id'] for lottery_data in self.pending_lotteries.values()]
        return max(pending_lottery_ids) + 1 if pending_lottery_ids else 1

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        message = reaction.message
        message_id = message.id
        emoji = reaction.emoji
        if message_id in Lottery.pending_lotteries:
            lottery_emoji_code = Lottery.pending_lotteries[message_id]['emoji_code']
            same_emoji = emoji == lottery_emoji_code if isinstance(emoji, str) else emoji.id == lottery_emoji_code
            if same_emoji and \
                    not user.bot and \
                    not checker.has_any_role(self.guild, user, Lottery.USER_ROLE_NAMES):
                try:
                    await utils.try_dm(
                        user, f"Vous devez avoir le rôle @{Lottery.USER_ROLE_NAMES[0]} pour "
                              f"participer à cette loterie."
                    )
                    await message.remove_reaction(emoji, user)
                except (
                        discord.errors.HTTPException,
                        discord.errors.NotFound,
                        discord.errors.Forbidden
                ):
                    pass

    @lottery.command(
        name='list',
        aliases=['l', 'ls'],
        brief="Affiche la liste des tirages au sort en cours",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def list(self, context: commands.Context):
        lottery_descriptions, guild_id = {}, self.guild.id
        for message_id, lottery_data in self.pending_lotteries.items():
            lottery_id = lottery_data['lottery_id']
            channel_id = lottery_data['channel_id']
            organizer = self.guild.get_member(lottery_data['organizer_id'])
            time = scheduler.get_job_run_date(lottery_data['_id'])
            message_link = f"https://discordapp.com/channels/{guild_id}/{channel_id}/{message_id}"
            lottery_descriptions[lottery_id] = f" • `[{lottery_id}]` - Programmé par {organizer.mention} " \
                                               f"pour le [__{converter.humanize_datetime(time)}__]({message_link})"
        embed_description = "Aucun" if not lottery_descriptions \
            else "\n".join([lottery_descriptions[lottery_id] for lottery_id in sorted(lottery_descriptions.keys())])
        embed = discord.Embed(
            title="Tirage(s) au sort en cours",
            description=embed_description,
            color=self.EMBED_COLOR
        )
        await context.send(embed=embed)

    @lottery.command(
        name='pick',
        aliases=['p', 'run', 'r'],
        usage="<lottery_id> [seed]",
        brief="Effectue un tirage au sort en cours",
        help="Force un tirage au sort à se dérouler à l'avance. Si un seed est fourni, le tirage au"
             "sort se base dessus pour le choix des gagnants.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def pick(self, context: commands.Context, lottery_id: int, seed: int = None):
        message, _, _, _, _, organizer = await self.get_message_env(
            lottery_id, raise_if_not_found=True
        )

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)

        await Lottery.run_lottery(message.id, seed=seed, manual_run=True)
        await context.send(f"Tirage au sort d'identifiant `{lottery_id}` exécuté : <{message.jump_url}>")

    @staticmethod
    async def run_lottery(message_id, seed=None, manual_run=False):
        lottery_id = Lottery.pending_lotteries[message_id]['lottery_id']
        message, channel, emoji, nb_winners, time, organizer = await Lottery.get_message_env(
            lottery_id
        )
        try:
            players, reaction, winners = await Lottery.draw(
                message, emoji, nb_winners, seed
            )
            await reaction.remove(zbot.bot.user)
            await Lottery.announce_winners(winners, players, message, organizer)
            Lottery.remove_pending_lottery(message_id, cancel_job=manual_run)
        except commands.CommandError as error:
            context = commands.Context(
                bot=zbot.bot,
                cog=Lottery,
                prefix=zbot.bot.command_prefix,
                channel=channel,
                message=message,
            )
            await error_handler.handle(context, error)

    @lottery.command(
        name='cancel',
        aliases=['c'],
        usage="<lottery_id>",
        brief="Annule le tirage au sort",
        help="Le numéro de loterie est affiché entre crochets par la commande `+lottery list`.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def cancel(self, context: commands.Context, lottery_id: int):
        message, _, emoji, _, _, organizer = await self.get_message_env(
            lottery_id, raise_if_not_found=False)

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)

        if message:
            reaction = utils.try_get(message.reactions, error=exceptions.MissingEmoji(emoji), emoji=emoji)
            await reaction.remove(zbot.bot.user)
            embed = discord.Embed(
                title=f"Tirage au sort __annulé__ par {context.author.display_name}",
                color=Lottery.EMBED_COLOR
            )
            embed.set_author(name=f"Organisateur : @{organizer.display_name}", icon_url=organizer.avatar_url)
            await message.edit(embed=embed)
        self.remove_pending_lottery(message.id, cancel_job=True)
        await context.send(f"Tirage au sort d'identifiant `{lottery_id}` annulé : <{message.jump_url}>")

    @lottery.group(
        name='edit',
        brief="Modifie un tirage au sort",
        invoke_without_command=True
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def edit(self, context):
        if context.invoked_subcommand is None:
            raise exceptions.MissingSubCommand(f'lottery {context.command.name}')

    @edit.command(
        name='announce',
        aliases=['annonce', 'a', 'description', 'desc', 'd', 'message', 'm'],
        usage="<lottery_id> <\"announce\"> [--no-announce]",
        brief="Modifie l'annonce du tirage au sort",
        help="La précédente annonce associée au tirage au sort est remplacée par le message fourni. "
             "Par défaut, la nouvelle annonce mentionne automatiquement le rôle `@Abonné Annonces`. "
             "Pour éviter cela, il faut ajouter l'argument `--no-announce`.\n"
             "Dans tous les cas, les membres du serveur ne seront pas notifiés.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def announce(
            self, context: commands.Context,
            lottery_id: int,
            announce: str,
            *, options=""
    ):
        message, channel, _, _, _, organizer = await self.get_message_env(
            lottery_id, raise_if_not_found=True
        )

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)
        if not context.author.permissions_in(channel).send_messages:
            raise exceptions.ForbiddenChannel(channel)

        do_announce = not utils.is_option_enabled(options, 'no-announce')
        prefixed_announce = utils.make_announce(
            context.guild, announce, do_announce and self.ANNOUNCE_ROLE_NAME
        )
        await message.edit(content=prefixed_announce)
        await context.send(
            f"Annonce du tirage au sort d'identifiant `{lottery_id}` remplacée par "
            f"\"`{message.clean_content}`\" : <{message.jump_url}>"
        )

    @edit.command(
        name='emoji',
        aliases=['émoji', 'e'],
        usage="<lottery_id> <:emoji:>",
        brief="Modifie l'émoji du tirage au sort",
        help="Le précédent émoji associé au tirage au sort est remplacé par l'émoji fourni. "
             "Les réactions à l'ancien émoji ne sont pas prises en compte.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def emoji(self, context: commands.Context, lottery_id: int, emoji: converter.to_emoji):
        message, channel, previous_emoji, nb_winners, time, organizer = \
            await self.get_message_env(lottery_id, raise_if_not_found=True)

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)

        previous_reaction = utils.try_get(
            message.reactions, error=exceptions.MissingEmoji(previous_emoji), emoji=previous_emoji
        )
        await previous_reaction.remove(zbot.bot.user)
        await message.add_reaction(emoji)
        embed = self.build_announce_embed(emoji, nb_winners, organizer, time, self.guild.roles)
        await message.edit(embed=embed)

        job_id = self.pending_lotteries[message.id]['_id']
        lottery_data = {'emoji_code': emoji if isinstance(emoji, str) else emoji.id}
        zbot.db.update_job_data(self.JOBSTORE, job_id, lottery_data)
        self.pending_lotteries[message.id].update(lottery_data)
        await context.send(
            f"Émoji du tirage au sort d'identifiant `{lottery_id}` remplacé par \"{emoji}\" : "
            f"<{message.jump_url}>"
        )

    @edit.command(
        name='organizer',
        aliases=['organisateur', 'org', 'o'],
        usage="<lottery_id> <@organizer>",
        brief="Modifie l'organisateur du tirage au sort",
        help="Le précédent organisateur du tirage au sort est remplacé par l'organisateur fourni.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def organizer(
            self, context: commands.Context,
            lottery_id: int,
            organizer: discord.User
    ):
        message, channel, emoji, nb_winners, time, previous_organizer = \
            await self.get_message_env(lottery_id, raise_if_not_found=True)

        if context.author != previous_organizer:
            checker.has_any_mod_role(context, print_error=True)

        embed = self.build_announce_embed(emoji, nb_winners, organizer, time, self.guild.roles)
        await message.edit(embed=embed)

        job_id = self.pending_lotteries[message.id]['_id']
        lottery_data = {'organizer_id': organizer.id}
        zbot.db.update_job_data(self.JOBSTORE, job_id, lottery_data)
        self.pending_lotteries[message.id].update(lottery_data)
        await context.send(
            f"Organisateur du tirage au sort d'identifiant `{lottery_id}` remplacé par "
            f"`@{organizer.display_name}` : <{message.jump_url}>"
        )

    @edit.command(
        name='time',
        aliases=['date', 'heure', 't'],
        usage="<lottery_id> <\"time\">",
        brief="Modifie la date et l'heure du tirage au sort",
        help="Le précédente date et heure du tirage au sort sont changées pour celles fournies (au "
             "format `\"YYYY-MM-DD HH:MM:SS\"`). ",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def time(
            self, context: commands.Context,
            lottery_id: int,
            time: converter.to_future_datetime
    ):
        message, channel, emoji, nb_winners, _, organizer = \
            await self.get_message_env(lottery_id, raise_if_not_found=True)

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)

        embed = self.build_announce_embed(emoji, nb_winners, organizer, time, self.guild.roles)
        await message.edit(embed=embed)

        job_id = self.pending_lotteries[message.id]['_id']
        scheduler.reschedule_stored_job(job_id, time)  # Also updates the next_run_time in db
        lottery_data = {'next_run_time': converter.to_timestamp(time)}
        self.pending_lotteries[message.id].update(lottery_data)
        await context.send(
            f"Date et heure du tirage au sort d'identifiant `{lottery_id}` changées pour le "
            f"`{converter.humanize_datetime(time)}` : <{message.jump_url}>"
        )

    @edit.command(
        name='winners',
        aliases=['gagnants', 'nb_winners', 'n'],
        usage="<lottery_id> <nb_winners>",
        brief="Modifie le nombre de gagnants du tirage au sort",
        help="Le précédent nombre de gagnants du tirage au sort est remplacé par le nombre de "
             "gagnants fourni.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def winners(
            self, context: commands.Context,
            lottery_id: int,
            nb_winners: converter.to_positive_int
    ):
        message, channel, emoji, _, time, organizer = \
            await self.get_message_env(lottery_id, raise_if_not_found=True)

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)

        embed = self.build_announce_embed(emoji, nb_winners, organizer, time, self.guild.roles)
        await message.edit(embed=embed)

        job_id = self.pending_lotteries[message.id]['_id']
        lottery_data = {'nb_winners': nb_winners}
        zbot.db.update_job_data(self.JOBSTORE, job_id, lottery_data)
        self.pending_lotteries[message.id].update(lottery_data)
        await context.send(
            f"Nombre de gagnants du tirage au sort d'identifiant `{lottery_id}` changé à "
            f"`{nb_winners}` : <{message.jump_url}>"
        )

    @staticmethod
    async def get_message_env(lottery_id: int, raise_if_not_found=True) -> \
            (discord.Message, discord.TextChannel, typing.Union[str, discord.Emoji],
             datetime.datetime, str, discord.Member):
        if not (lottery_data := discord.utils.find(
                lambda data: data['lottery_id'] == lottery_id,
                Lottery.pending_lotteries.values()
        )):
            raise exceptions.UnknownLottery(lottery_id)

        channel = zbot.bot.get_channel(lottery_data['channel_id'])
        message = await utils.try_get_message(
            channel, lottery_data['message_id'],
            error=exceptions.MissingMessage(lottery_data['message_id'])
            if raise_if_not_found else None
        )
        emoji = utils.try_get_emoji(zbot.bot.emojis, lottery_data['emoji_code'], error=None)  # TODO cancel lottery if not found
        nb_winners = lottery_data['nb_winners']
        time = converter.from_timestamp(lottery_data['next_run_time'])
        organizer = zbot.bot.get_user(lottery_data['organizer_id'])

        return message, channel, emoji, nb_winners, time, organizer

    @staticmethod
    def remove_pending_lottery(message_id, cancel_job=False):
        if message_id not in Lottery.pending_lotteries:
            return  # Callback of misfired lottery or manual run
        for lottery_data in Lottery.pending_lotteries.values():
            if lottery_data['lottery_id'] > Lottery.pending_lotteries[message_id]['lottery_id']:
                lottery_data['lottery_id'] -= 1
                zbot.db.update_job_data(
                    Lottery.JOBSTORE,
                    lottery_data['_id'],
                    {'lottery_id': lottery_data['lottery_id']}
                )
        if cancel_job:
            scheduler.cancel_stored_job(Lottery.pending_lotteries[message_id]['_id'])
        del Lottery.pending_lotteries[message_id]

    @lottery.command(
        name='simulate',
        aliases=['sim'],
        usage="<#src_channel> <message_id> [:emoji:] [nb_winners] [#dest_channel] [@organizer] "
              "[seed]",
        brief="Simule un tirage au sort",
        help="Le bot tire au sort le **nombre de gagnants** parmi les joueurs ayant réagi au "
             "**message source** avec l'émoji de la réaction présente si elle est unique, avec "
             "l'**émoji** fourni sinon. Si un **canal de destination** est fourni, il est utilisé "
             "pour publier les résultats de la simulation. Sinon, le canal courant est utilisé. Si "
             "un **organisateur** est indiqué, les gagnants sont contactés par MP et "
             "l'organisateur reçoit par MP une copie du résultat et de la liste des participants "
             "injoignables. Si un **seed** est fourni, la simulation se base dessus pour le choix "
             "des gagnants.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def simulate(
            self, context: commands.Context,
            src_channel: discord.TextChannel,
            message_id: int,
            emoji: converter.to_emoji = None,
            nb_winners: converter.to_positive_int = 1,
            dest_channel: discord.TextChannel = None,
            organizer: discord.User = None,
            seed: int = None
    ):
        if dest_channel and not context.author.permissions_in(dest_channel).send_messages:
            raise exceptions.ForbiddenChannel(dest_channel)

        message = await utils.try_get_message(
            src_channel, message_id, error=exceptions.MissingMessage(message_id)
        )
        if not emoji:
            if len(message.reactions) != 1:
                raise exceptions.MissingConditionalArgument(
                    "Un émoji doit être fourni si le message ciblé n'a pas exactement une réaction."
                )
            else:
                emoji = message.reactions[0].emoji
        players, reaction, winners = await Lottery.draw(
            message, emoji, nb_winners, seed=seed
        )
        announce = f"Tirage au sort sur base de la réaction {emoji}" \
                   f"{f' et du seed `{seed}`' if seed else ''} au message {message.jump_url}"
        announcement = await (dest_channel or context).send(announce)
        await Lottery.announce_winners(
            winners, players, announcement, organizer=organizer
        )

    @staticmethod
    async def draw(message, emoji, nb_winners, seed=None):
        await Lottery.prepare_seed(seed)
        reaction = utils.try_get(message.reactions, error=exceptions.MissingEmoji(emoji), emoji=emoji)
        players, winners = await Lottery.pick_winners(message.guild, reaction, nb_winners)
        return players, reaction, winners

    @staticmethod
    async def prepare_seed(default_seed=None):
        seed = default_seed if default_seed else random.randrange(10 ** 6)  # 6 digits seed
        random.seed(seed)
        logger.debug(f"Picking winners using seed = {seed} ({utils.get_current_time()})")

    @staticmethod
    async def pick_winners(guild, reaction, nb_winners, ignore_roles=False):
        players = list(filter(
            lambda m: checker.has_any_role(guild, m, Lottery.USER_ROLE_NAMES)
            or ignore_roles and not m.bot,
            await reaction.users().flatten())
        )
        nb_winners = min(nb_winners, len(players))
        winners = random.sample(players, nb_winners)
        return players, winners

    @staticmethod
    async def announce_winners(
            winners: [discord.User], players: [discord.User], message,
            organizer: discord.User = None
    ):
        embed = discord.Embed(
            title="Résultats du tirage au sort 🎉",
            description=f"Gagnant(s) parmi {len(players)} participant(s):\n" +
                        utils.make_user_list(winners, "\n"),
            color=Lottery.EMBED_COLOR
        )
        if organizer:
            embed.set_author(
                name=f"Organisateur : @{organizer.display_name}", icon_url=organizer.avatar_url)
        await message.edit(embed=embed)

        if organizer:
            # DM winners
            unreachable_winners = []
            for winner in winners:
                if not await utils.try_dm(
                    winner, f"Félicitations ! Tu as été tiré au sort lors de la loterie organisée "
                            f"par {organizer.display_name} ({organizer.mention}) !\n"
                            f"Contacte cette personne par MP pour obtenir ta récompense :wink:" +
                            f"\nLien : {message.jump_url}"
                ):
                    unreachable_winners.append(winner)
            # DM organizer
            winner_list = utils.make_user_list(winners)
            await utils.try_dm(organizer, f"Les gagnants de la loterie sont: {winner_list}\n"
                                          f"Lien : {message.jump_url}")
            if unreachable_winners:
                unreachable_winner_list = utils.make_user_list(unreachable_winners)
                await utils.try_dm(organizer, f"Les gagnants suivants ont bloqué les MPs et n'ont "
                                              f"pas pu être contactés: {unreachable_winner_list}")
            # Log players and winners
            player_list = utils.make_user_list(players, mention=False)
            winner_list = utils.make_user_list(winners, mention=False)
            logger.debug(f"Players: {player_list}")
            logger.debug(f"Winners: {winner_list}")


def setup(bot):
    bot.add_cog(Lottery(bot))
