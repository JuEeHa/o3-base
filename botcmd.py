# initialize(*, config)
# Called to initialize the IRC bot
# Runs before even logger is brought up, and blocks further bringup until it's done
# config is a configpatser.ConfigParser object containig contents of bot.conf
def initialize(*, config):
	...

# on_connect(*, irc)
# Called after IRC bot has connected and sent the USER/NICk commands but not yet attempted anything else
# Called for every reconnect
# Blocks the bot until it's done, including PING/PONG handling
# irc is the IRC API object
def on_connect(*, irc):
	...

# on_quit(*, irc)
# Called just before IRC bot sends QUIT
# Blocks the bot until it's done, including PING/PONG handling
# irc is the IRC API object
def on_quit(*, irc):
	...

# handle_message(*, prefix, message, nick, channel, irc)
# Called for PRIVMSGs.
# prefix is the prefix at the start of the message, without the leading ':'
# message is the contents of the message
# nick is who sent the message
# channel is where you should send the response (note: in queries nick == channel)
# irc is the IRC API object
# All strings are bytestrings
def handle_message(*, prefix, message, nick, channel, irc):
	...

# handle_nonmessage(*, prefix, command, arguments, irc)
# Called for all other commands than PINGs and PRIVMSGs.
# prefix is the prefix at the start of the message, without the leading ':'
# command is the command or number code
# arguments is rest of the arguments of the command, represented as a list. ':'-arguments are handled automatically
# irc is the IRC API object
# All strings are bytestrings
def handle_nonmessage(*, prefix, command, arguments, irc):
	...
