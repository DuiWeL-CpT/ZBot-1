import datetime
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


class Poll(_command.Command):

    """Commands for management of polls."""

    DISPLAY_NAME = "Sondages"
    DISPLAY_SEQUENCE = 4
    MOD_ROLE_NAMES = ['Administrateur', 'Modérateur']
    USER_ROLE_NAMES = ['Joueur']

    ANNOUNCE_ROLE_NAME = 'Abonné Annonces'
    EMBED_COLOR = 0x9D71DC  # Pastel purple
    JOBSTORE = database.MongoDBConnector.PENDING_POLLS_COLLECTION

    pending_polls = {}  # Local cache of poll data for reactions check

    def __init__(self, bot):
        super().__init__(bot)
        # Use class attribute to be available from static methods
        Poll.pending_polls = zbot.db.load_pending_jobs_data(
            self.JOBSTORE,
            data_keys=(
                '_id', 'poll_id', 'message_id', 'channel_id', 'emoji_codes', 'next_run_time',
                'organizer_id', 'is_exclusive', 'required_role_name'
            )
        )

    @commands.group(
        name='poll',
        brief="Gère les sondages",
        invoke_without_command=True
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def poll(self, context):
        if context.invoked_subcommand is None:
            raise exceptions.MissingSubCommand(context.command.name)

    @poll.command(
        name='start',
        aliases=['s'],
        usage="<\"announce\"> <\"description\"> <#dest_channel> <\":emoji1: :emoji2: ...\"> "
              "<\"time\"> [--exclusive] [--role=\"name\"] [--do-announce] [--pin]",
        brief="Démarre un sondage",
        help="Le bot publie un sondage sous forme d'un message correspondant à l'**annonce** et "
             "d'un embed contenant la **description** dans le **canal de destination**. Les "
             "joueurs participent en cliquant sur le ou les **émojis** de réaction (séparés par un "
             "espace). À la **date et heure** indiquées (au format `\"YYYY-MM-DD HH:MM:SS\"`), "
             "les résultats sont affichés dans un second message. Par défaut, le sondage est à "
             "choix multiple et il n'y a aucune restriction de rôle. Pour changer cela, il faut "
             "respectivement ajouter les arguments `--exclusive` et `--role=\"Nom de rôle\"`. Pour "
             "automatiquement mentionner le rôle `@Abonné Annonces`, ajoutez l'argument "
             "`--do-announce`. Pour épingler automatiquement l'annonce, ajoutez l'argument "
             "`--pin`.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def start(
            self, context: commands.Context,
            announce: str,
            description: str,
            dest_channel: discord.TextChannel,
            emoji_list: converter.to_emoji_list,
            time: converter.to_future_datetime,
            *, options=""
    ):
        # Check arguments
        if not context.author.permissions_in(dest_channel).send_messages:
            raise exceptions.ForbiddenChannel(dest_channel)
        if not emoji_list:
            raise commands.MissingRequiredArgument(context.command.params['emoji_list'])
        do_announce = utils.is_option_enabled(options, 'do-announce')
        do_pin = utils.is_option_enabled(options, 'pin')
        if do_announce or do_pin:
            checker.has_any_mod_role(context, print_error=True)
        required_role_name = utils.get_option_value(options, 'role')
        if required_role_name:
            utils.try_get(  # Raise if role does not exist
                self.guild.roles, error=exceptions.UnknownRole(required_role_name),
                name=required_role_name
            )

        # Run command
        is_exclusive = utils.is_option_enabled(options, 'exclusive')
        organizer = context.author
        prefixed_announce = utils.make_announce(
            context.guild, announce, do_announce and self.ANNOUNCE_ROLE_NAME)
        embed = self.build_announce_embed(
            description, is_exclusive, required_role_name, organizer, time, self.guild.roles)
        message = await dest_channel.send(prefixed_announce, embed=embed)
        for emoji in emoji_list:
            await message.add_reaction(emoji)
        if do_pin:
            await message.pin()

        # Register data
        job_id = scheduler.schedule_stored_job(self.JOBSTORE, time, self.close_poll, message.id).id
        poll_data = {
            'poll_id': self.get_next_poll_id(),
            'message_id': message.id,
            'channel_id': dest_channel.id,
            'emoji_codes': list(map(lambda e: e if isinstance(e, str) else e.id, emoji_list)),
            'organizer_id': organizer.id,
            'is_exclusive': is_exclusive,
            'required_role_name': required_role_name,
        }
        zbot.db.update_job_data(self.JOBSTORE, job_id, poll_data)
        # Add data managed by scheduler later to avoid updating the database with them
        poll_data.update({'_id': job_id, 'next_run_time': converter.to_timestamp(time)})
        self.pending_polls[message.id] = poll_data

        # Confirm command
        await context.send(f"Sondage d'identifiant `{poll_data['poll_id']}` programmé : <{message.jump_url}>.")

    @staticmethod
    def build_announce_embed(
            description, is_exclusive, required_role_name, organizer, time, guild_roles
    ):
        embed = discord.Embed(
            title=f"Clôture du sondage le {converter.humanize_datetime(time)} :alarm_clock:",
            description=description,
            color=Poll.EMBED_COLOR,
        )
        embed.add_field(
            name="Participation",
            value="Réagissez avec un émoji ci-dessous." if is_exclusive
            else "Réagissez avec les émojis ci-dessous."
        )
        embed.add_field(
            name="Choix multiple",
            value="✅" if not is_exclusive else "❌"
        )
        embed.add_field(
            name="Rôle requis",
            value=utils.try_get(
                guild_roles,
                error=exceptions.UnknownRole(required_role_name),
                name=required_role_name
            ).mention if required_role_name else "Aucun"
        )
        embed.set_author(
            name=f"Organisateur : @{organizer.display_name}",
            icon_url=organizer.avatar_url
        )
        return embed

    def get_next_poll_id(self) -> int:
        pending_poll_ids = [poll_data['poll_id'] for poll_data in self.pending_polls.values()]
        return max(pending_poll_ids) + 1 if pending_poll_ids else 1

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        message = reaction.message
        message_id = message.id
        emoji = reaction.emoji
        if message_id in Poll.pending_polls:
            poll_emoji_codes = Poll.pending_polls[message_id]['emoji_codes']
            same_emoji = any([
                emoji == poll_emoji_code if isinstance(emoji, str) else emoji.id == poll_emoji_code
                for poll_emoji_code in poll_emoji_codes
            ])
            if same_emoji and not user.bot:
                if required_role_name := Poll.pending_polls[message_id]['required_role_name']:
                    if not checker.has_guild_role(self.guild, user, required_role_name):
                        try:
                            await utils.try_dm(
                                user, f"Vous devez avoir le rôle @{required_role_name} pour "
                                      f"participer à ce sondage."
                            )
                            await message.remove_reaction(emoji, user)
                            return  # Don't enter other check blocks
                        except (
                                discord.errors.HTTPException,
                                discord.errors.NotFound,
                                discord.errors.Forbidden
                        ):
                            pass
                if Poll.pending_polls[message_id]['is_exclusive']:
                    for existing_reaction in list(filter(
                        lambda r: r.emoji != emoji and
                        (r.emoji if isinstance(r.emoji, str) else r.emoji.id) in poll_emoji_codes,
                        message.reactions
                    )):
                        existing_reaction_users = await existing_reaction.users().flatten()
                        if discord.utils.get(existing_reaction_users, id=user.id):
                            try:
                                await utils.try_dm(
                                    user, f"Vous ne pouvez voter que pour une seule option."
                                )
                                await message.remove_reaction(emoji, user)
                                return  # Don't enter other check blocks
                            except (
                                    discord.errors.HTTPException,
                                    discord.errors.NotFound,
                                    discord.errors.Forbidden
                            ):
                                pass
                            break

    @poll.command(
        name='list',
        aliases=['l', 'ls'],
        brief="Affiche la liste des sondages en cours",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def list(self, context: commands.Context):
        poll_descriptions, guild_id = {}, self.guild.id
        for message_id, poll_data in self.pending_polls.items():
            poll_id = poll_data['poll_id']
            channel_id = poll_data['channel_id']
            organizer = self.guild.get_member(poll_data['organizer_id'])
            time = scheduler.get_job_run_date(poll_data['_id'])
            message_link = f"https://discordapp.com/channels/{guild_id}/{channel_id}/{message_id}"
            poll_descriptions[poll_id] = f" • `[{poll_id}]` - Démarré par {organizer.mention} " \
                                         f"jusqu'au [__{converter.humanize_datetime(time)}__]({message_link})"
        embed_description = "Aucun" if not poll_descriptions \
            else "\n".join([poll_descriptions[poll_id] for poll_id in sorted(poll_descriptions.keys())])
        embed = discord.Embed(
            title="Sondage(s) en cours",
            description=embed_description,
            color=self.EMBED_COLOR
        )
        await context.send(embed=embed)

    @poll.command(
        name='assess',
        aliases=['a', 'close'],
        usage="<poll_id>",
        brief="Évalue un sondage en cours",
        help="Force un sondage à se terminer à l'avance.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def assess(self, context: commands.Context, poll_id: int):
        message, _, _, _, _, _, organizer = await self.get_message_env(
            poll_id, raise_if_not_found=True
        )

        if context.author.id != organizer:
            checker.has_any_mod_role(context, print_error=True)

        await Poll.close_poll(message.id, manual_run=True)
        await context.send(f"Sondage d'identifiant `{poll_id}` clôturé : <{message.jump_url}>")

    @staticmethod
    async def close_poll(message_id, manual_run=False):
        poll_id = Poll.pending_polls[message_id]['poll_id']
        message, channel, emoji_list, is_exclusive, required_role_name, time, organizer = \
            await Poll.get_message_env(poll_id)
        try:
            reactions, results = await Poll.count_votes(
                message, emoji_list, is_exclusive, required_role_name)
            for reaction in reactions:
                await reaction.remove(zbot.bot.user)
            await message.unpin()
            await Poll.announce_results(
                results, message, channel, is_exclusive, required_role_name, organizer
            )
            Poll.remove_pending_poll(message_id, cancel_job=manual_run)
        except commands.CommandError as error:
            context = commands.Context(
                bot=zbot.bot,
                cog=Poll,
                prefix=zbot.bot.command_prefix,
                channel=channel,
                message=message,
            )
            await error_handler.handle(context, error)

    @poll.command(
        name='cancel',
        aliases=['c'],
        usage="<poll_id>",
        brief="Annule le sondage",
        help="Le numéro de sondage est affiché entre crochets par la commande `+poll list`.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def cancel(self, context: commands.Context, poll_id: int):
        message, _, emoji_list, _, _, _, organizer = await self.get_message_env(
            poll_id, raise_if_not_found=False)
        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)
        if message:
            for emoji in emoji_list:
                reaction = utils.try_get(
                    message.reactions, error=exceptions.MissingEmoji(emoji), emoji=emoji
                )
                await reaction.remove(zbot.bot.user)
            embed = discord.Embed(
                title=f"Sondage __annulé__ par {context.author.display_name}",
                description=message.embeds[0].description if message.embeds[0].description else "",
                color=Poll.EMBED_COLOR
            )
            embed.set_author(name=f"Organisateur : @{organizer.display_name}", icon_url=organizer.avatar_url)
            await message.edit(embed=embed)
            await message.unpin()
        self.remove_pending_poll(message.id, cancel_job=True)
        await context.send(f"Sondage d'identifiant `{poll_id}` annulé : <{message.jump_url}>")

    @poll.group(
        name='edit',
        brief="Modifie un sondage",
        invoke_without_command=True
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def edit(self, context):
        if context.invoked_subcommand is None:
            raise exceptions.MissingSubCommand(f'poll {context.command.name}')

    @edit.command(
        name='announce',
        aliases=['annonce', 'a'],
        usage="<poll_id> <\"announce\"> [--do-announce] [--pin]",
        brief="Modifie l'annonce du sondage",
        help="La précédente annonce associée au sondage est remplacée par le message fourni. Pour "
             "que la nouvelle annonce mentionne automatiquement le rôle `@Abonné Annonces`, "
             "ajoutez l'argument `--do-announce` (que la précédente annonce le fasse déjà ou pas ; "
             "dans tous les cas, les membres du serveur ne seront pas notifiés). Pour épingler "
             "automatiquement l'annonce, ajoutez l'argument `--pin` (si pas spécifié et si la "
             "précédente annonce était épinglée, elle sera désépinglée).",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def announce(
            self, context: commands.Context,
            poll_id: int,
            announce: str,
            *, options=""
    ):
        message, channel, _, _, _, _, organizer = await self.get_message_env(
            poll_id, raise_if_not_found=True
        )

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)
        if not context.author.permissions_in(channel).send_messages:
            raise exceptions.ForbiddenChannel(channel)
        do_announce = utils.is_option_enabled(options, 'do-announce')
        do_pin = utils.is_option_enabled(options, 'pin')
        if do_announce or do_pin:
            checker.has_any_mod_role(context, print_error=True)

        prefixed_announce = utils.make_announce(
            context.guild, announce, do_announce and self.ANNOUNCE_ROLE_NAME
        )
        if do_pin and not message.pinned:
            await message.pin()
        elif not do_pin and message.pinned:
            await message.unpin()
        await message.edit(content=prefixed_announce)
        await context.send(
            f"Annonce du sondage d'identifiant `{poll_id}` remplacée par "
            f"\"`{message.clean_content}`\" : <{message.jump_url}>"
        )

    @edit.command(
        name='description',
        aliases=['desc', 'd', 'message', 'm'],
        usage="<poll_id> <\"description\">",
        brief="Modifie la description du sondage",
        help="La précédente description présente dans l'embed du sondage est remplacée par le "
             "message fourni.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def description(self, context: commands.Context, poll_id: int, description: str):
        message, channel, _, is_exclusive, required_role_name, time, organizer = \
            await self.get_message_env(poll_id, raise_if_not_found=True)

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)
        if not context.author.permissions_in(channel).send_messages:
            raise exceptions.ForbiddenChannel(channel)

        embed = self.build_announce_embed(
            description, is_exclusive, required_role_name, organizer, time, self.guild.roles)
        await message.edit(embed=embed)
        await context.send(
            f"Description du sondage d'identifiant `{poll_id}` remplacée par "
            f"\"`{description}`\" : <{message.jump_url}>"
        )

    @edit.command(
        name='emojis',
        aliases=['émojis', 'e'],
        usage="<poll_id> <\":emoji1: :emoji2: ...\"> [--exclusive] [--role=\"name\"]",
        brief="Modifie les émojis du sondage",
        help="Les précédents émojis associés au sondage sont remplacés par le ou les émojis "
             "fournis (séparés par un espace). Par défaut, le sondage est à choix multiple et il "
             "n'y a aucune restriction de rôle. Pour changer cela, il faut respectivement ajouter "
             "les arguments `--exclusive` et `--role=\"Nom de rôle\"`. Les réactions aux anciens "
             "émojis non repris ne sont pas prises en compte.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def emojis(
            self, context: commands.Context,
            poll_id: int,
            emoji_list: converter.to_emoji_list,
            *, options=""
    ):
        message, channel, previous_emoji_list, is_exclusive, required_role_name, time, organizer = \
            await self.get_message_env(poll_id, raise_if_not_found=True)

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)
        if not context.author.permissions_in(channel).send_messages:
            raise exceptions.ForbiddenChannel(channel)
        if not emoji_list:
            raise commands.MissingRequiredArgument(context.command.params['emoji_list'])
        required_role_name = utils.get_option_value(options, 'role')
        if required_role_name:
            utils.try_get(  # Raise if role does not exist
                self.guild.roles, error=exceptions.UnknownRole(required_role_name),
                name=required_role_name
            )

        is_exclusive = utils.is_option_enabled(options, 'exclusive')
        previous_reactions = [
            utils.try_get(
                message.reactions,
                error=exceptions.MissingEmoji(previous_emoji),
                emoji=previous_emoji
            )
            for previous_emoji in previous_emoji_list
        ]
        for previous_reaction in previous_reactions:
            await previous_reaction.remove(zbot.bot.user)
        for emoji in emoji_list:
            await message.add_reaction(emoji)
        embed = self.build_announce_embed(
            message.embeds[0].description, is_exclusive, required_role_name, organizer, time,
            self.guild.roles
        )
        await message.edit(embed=embed)

        job_id = self.pending_polls[message.id]['_id']
        poll_data = {
            'emoji_codes': list(map(lambda e: e if isinstance(e, str) else e.id, emoji_list)),
            'is_exclusive': is_exclusive,
            'required_role_name': required_role_name
        }
        zbot.db.update_job_data(self.JOBSTORE, job_id, poll_data)
        self.pending_polls[message.id].update(poll_data)
        await context.send(
            f"Émojis du sondage d'identifiant `{poll_id}` mis à jour : <{message.jump_url}>"
        )

    @edit.command(
        name='organizer',
        aliases=['organisateur', 'org', 'o'],
        usage="<poll_id> <@organizer>",
        brief="Modifie l'organisateur du sondage",
        help="Le précédent organisateur du sondage est remplacé par l'organisateur fourni.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def organizer(
            self, context: commands.Context,
            poll_id: int,
            organizer: discord.User
    ):
        message, channel, emoji_list, is_exclusive, required_role_name, time, previous_organizer = \
            await self.get_message_env(poll_id, raise_if_not_found=True)

        if context.author != previous_organizer:
            checker.has_any_mod_role(context, print_error=True)
        if not context.author.permissions_in(channel).send_messages:
            raise exceptions.ForbiddenChannel(channel)

        embed = self.build_announce_embed(
            message.embeds[0].description, is_exclusive, required_role_name, organizer, time,
            self.guild.roles
        )
        await message.edit(embed=embed)

        job_id = self.pending_polls[message.id]['_id']
        poll_data = {'organizer_id': organizer.id}
        zbot.db.update_job_data(self.JOBSTORE, job_id, poll_data)
        self.pending_polls[message.id].update(poll_data)
        await context.send(
            f"Organisateur du sondage d'identifiant `{poll_id}` remplacé par "
            f"`@{organizer.display_name}` : <{message.jump_url}>"
        )

    @edit.command(
        name='time',
        aliases=['date', 'heure', 't'],
        usage="<poll_id> <\"time\">",
        brief="Modifie la date et l'heure du sondage",
        help="Les précédentes date et heure du sondage sont changées pour celles fournies (au "
             "format `\"YYYY-MM-DD HH:MM:SS\"`). ",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def time(
            self, context: commands.Context,
            poll_id: int,
            time: converter.to_future_datetime
    ):
        message, channel, emoji_list, is_exclusive, required_role_name, _, organizer = \
            await self.get_message_env(poll_id, raise_if_not_found=True)

        if context.author != organizer:
            checker.has_any_mod_role(context, print_error=True)
        if not context.author.permissions_in(channel).send_messages:
            raise exceptions.ForbiddenChannel(channel)

        embed = self.build_announce_embed(
            message.embeds[0].description, is_exclusive, required_role_name, organizer, time,
            self.guild.roles
        )
        await message.edit(embed=embed)

        job_id = self.pending_polls[message.id]['_id']
        scheduler.reschedule_stored_job(job_id, time)  # Also updates the next_run_time in db
        poll_data = {'next_run_time': converter.to_timestamp(time)}
        self.pending_polls[message.id].update(poll_data)
        await context.send(
            f"Date et heure du sondage d'identifiant `{poll_id}` changées pour le "
            f"`{converter.humanize_datetime(time)}` : <{message.jump_url}>"
        )

    @staticmethod
    async def get_message_env(poll_id: int, raise_if_not_found=True) -> \
            (discord.Message, discord.TextChannel, typing.List[typing.Union[str, discord.Emoji]],
             bool, bool, datetime.datetime, discord.Member):
        if not (poll_data := discord.utils.find(
                lambda data: data['poll_id'] == poll_id,
                Poll.pending_polls.values()
        )):
            raise exceptions.UnknownPoll(poll_id)
        channel = zbot.bot.get_channel(poll_data['channel_id'])
        message = await utils.try_get_message(
            channel, poll_data['message_id'],
            error=exceptions.MissingMessage(poll_data['message_id']) if raise_if_not_found else None
        )
        emoji_list = []
        for emoji_code in poll_data['emoji_codes']:
            emoji = utils.try_get_emoji(zbot.bot.emojis, emoji_code, error=None)
            if emoji:
                emoji_list.append(emoji)
            else:
                logger.warning(f"Custom emoji with id `{emoji_code}` not found.")
        is_exclusive = poll_data['is_exclusive']
        required_role_name = poll_data['required_role_name']
        time = converter.from_timestamp(poll_data['next_run_time'])
        organizer = zbot.bot.get_user(poll_data['organizer_id'])
        return message, channel, emoji_list, is_exclusive, required_role_name, time, organizer

    @staticmethod
    def remove_pending_poll(message_id, cancel_job=False):
        if message_id not in Poll.pending_polls:
            return  # Callback of misfired poll or manual run
        for poll_data in Poll.pending_polls.values():
            if poll_data['poll_id'] > Poll.pending_polls[message_id]['poll_id']:
                poll_data['poll_id'] -= 1
                zbot.db.update_job_data(
                    Poll.JOBSTORE,
                    poll_data['_id'],
                    {'poll_id': poll_data['poll_id']}
                )
        if cancel_job:
            scheduler.cancel_stored_job(Poll.pending_polls[message_id]['_id'])
        del Poll.pending_polls[message_id]

    @poll.command(
        name='simulate',
        aliases=['sim'],
        usage="<#src_channel> <message_id> [\":emoji1: :emoji2: ...\"] [#dest_channel] "
              "[--exclusive] [--role=\"name\"]",
        brief="Simule un sondage",
        help="Le bot compte les votes avec les émojis des réactions au **message source** s'il y "
             "en a, avec les **émojis fournis** (séparés par un espace) sinon. Si un **canal de "
             "destination** est fourni, il est utilisé pour publier les résultats de la "
             "simulation. Sinon, le canal courant est utilisé. Par défaut, le sondage est à choix "
             "multiple et il n'y a aucune restriction de rôle. Pour changer cela, il faut "
             "respectivement ajouter les arguments `--exclusive` et `--role=\"Nom de rôle\"`.",
        ignore_extra=False
    )
    @commands.check(checker.has_any_user_role)
    @commands.check(checker.is_allowed_in_current_guild_channel)
    async def simulate(
            self, context: commands.Context,
            src_channel: discord.TextChannel,
            message_id: int,
            emoji_list: converter.to_emoji_list = (),
            dest_channel: discord.TextChannel = None,
            *, options=""
    ):
        if dest_channel and not context.author.permissions_in(dest_channel).send_messages:
            raise exceptions.ForbiddenChannel(dest_channel)
        is_exclusive = utils.is_option_enabled(options, 'exclusive')
        required_role_name = utils.get_option_value(options, 'role')
        if required_role_name:
            utils.try_get(  # Raise if role does not exist
                self.guild.roles, error=exceptions.UnknownRole(required_role_name),
                name=required_role_name
            )

        message = await utils.try_get_message(
            src_channel, message_id, error=exceptions.MissingMessage(message_id)
        )
        if not emoji_list:
            if len(message.reactions) == 0:
                raise exceptions.MissingConditionalArgument(
                    "Une liste d'émojis doit être fournie si le message ciblé n'a pas de réaction."
                )
            else:
                emoji_list = [reaction.emoji for reaction in message.reactions]

        reactions, results = await Poll.count_votes(
            message, emoji_list, is_exclusive, required_role_name
        )
        announcement = await (dest_channel or context).send(
            "Évaluation des votes sur base des réactions."
        )
        await Poll.announce_results(
            results, message, announcement.channel, is_exclusive, required_role_name,
            dest_message=announcement
        )

    @staticmethod
    async def count_votes(message, emoji_list, is_exclusive, required_role_name):
        """Count the votes and compute the results of the poll."""
        reactions = [
            utils.try_get(message.reactions, error=exceptions.MissingEmoji(emoji), emoji=emoji)
            for emoji in emoji_list
        ]
        votes = {reaction.emoji: await reaction.users().flatten() for reaction in reactions}
        results = {}
        for emoji, voters in votes.items():
            valid_votes_count = 0
            for voter in voters:
                if voter.bot:
                    continue  # Exclude bot votes
                if is_exclusive and any([voter in votes[other_emoji] for other_emoji in votes
                                         if other_emoji != emoji]):
                    continue  # Only count vote of voters having voted once in exclusive mode
                if required_role_name and not checker.has_guild_role(
                        message.guild, voter, required_role_name
                ):
                    continue  # Only count vote of voters having the required role, if set
                valid_votes_count += 1
            results[emoji] = valid_votes_count
        return reactions, results

    @staticmethod
    async def announce_results(
            results: dict, src_message: discord.Message, channel: discord.TextChannel, is_exclusive,
            required_role_name, organizer: discord.User = None, dest_message: discord.Message = None
    ):
        announcement_embed = discord.Embed(
            title="Résultats du sondage",
            description=f"[Cliquez ici pour accéder au sondage 🗳️]({src_message.jump_url})",
            color=Poll.EMBED_COLOR
        )
        # Compute ranking of votes count: {votes_count: position, votes_count: position, ...}
        votes_ranking = {k: v for v, k in enumerate(sorted(set(results.values()), reverse=True), start=1)}
        # Compute ranking of emojis: {emoji: position, emoji: position, ...}
        emoji_ranking = {emoji: votes_ranking[vote_count] for emoji, vote_count in results.items()}
        ranking_medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for emoji, vote_count in results.items():
            rank = emoji_ranking[emoji]
            medal_message = f" {ranking_medals.get(rank, '')}" if vote_count > 0 else ""
            announcement_embed.add_field(
                name=f"{emoji}\u2000**# {rank}**",
                value=f"Votes: **{vote_count}**{medal_message}"
            )
        if organizer:
            announcement_embed.set_author(
                name=f"Organisateur : @{organizer.display_name}", icon_url=organizer.avatar_url)
        if dest_message:  # Poll simulation
            await dest_message.edit(embed=announcement_embed)
        else:  # Assessment of a poll
            dest_message = await channel.send(embed=announcement_embed)

            embed = discord.Embed(
                title="Sondage clôturé",
                description=f"[Cliquez ici pour accéder aux résultats 📊]({dest_message.jump_url})"
                            + (f"\n\n{src_message.embeds[0].description}"
                               if src_message.embeds[0].description else ""),
                color=Poll.EMBED_COLOR
            )
            embed.add_field(name="Choix multiple", value="✅" if not is_exclusive else "❌")
            embed.add_field(
                name="Rôle requis",
                value=utils.try_get(
                    src_message.guild.roles,
                    error=exceptions.UnknownRole(required_role_name),
                    name=required_role_name
                ).mention if required_role_name else "Aucun"
            )
            if organizer:
                embed.set_author(
                    name=f"Organisateur : @{organizer.display_name}", icon_url=organizer.avatar_url)
            await src_message.edit(embed=embed)

        logger.debug(f"Poll results: {results}")


def setup(bot):
    bot.add_cog(Poll(bot))
