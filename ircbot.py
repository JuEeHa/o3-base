#!/usr/bin/env python3
import configparser
import select
import socket
import threading
import time
from collections import namedtuple

import channel
from constants import logmessage_types, internal_submessage_types, controlmessage_types

import botcmd
import cron
import line_handling

Server = namedtuple('Server', ['host', 'port', 'nick', 'username', 'realname', 'channels'])

class LoggerThread(threading.Thread):
	def __init__(self, logging_channel, dead_notify_channel):
		self.logging_channel = logging_channel
		self.dead_notify_channel = dead_notify_channel

		threading.Thread.__init__(self)

	def run(self):
		while True:
			message_type, *message_data = self.logging_channel.recv()

			# Lines that were sent between server and client
			if message_type == logmessage_types.sent:
				assert len(message_data) == 1
				print('>' + message_data[0])

			elif message_type == logmessage_types.received:
				assert len(message_data) == 1
				print('<' + message_data[0])

			# Messages that are from internal components
			elif message_type == logmessage_types.internal:
				if message_data[0] == internal_submessage_types.quit:
					assert len(message_data) == 1
					print('--- Quit')

					self.dead_notify_channel.send((controlmessage_types.quit,))
					break

				elif message_data[0] == internal_submessage_types.error:
					assert len(message_data) == 2
					print('--- Error', message_data[1])

				else:
					print('--- ???', message_data)

			# Messages about status from the bot code
			elif message_type == logmessage_types.status:
				assert len(message_data) == 2
				print('*', end='')
				print(*message_data[0], **message_data[1])

			else:
				print('???', message_type, message_data)

# API(serverthread_object)
# Create a new API object corresponding to given ServerThread object
class API:
	def __init__(self, serverthread_object):
		# We need to access the internal functions of the ServerThread object in order to send lines etc.
		self.serverthread_object = serverthread_object

		# Have the cron object accessible more easily
		self.cron = serverthread_object.cron_control_channel

	def send_raw(self, line):
		"""Sends a raw line (will terminate it itself.)
		Don't use unless you are completely sure you know what you're doing."""
		self.serverthread_object.send_line_raw(line)

	def msg(self, recipient, message):
		"""Make sending PRIVMSGs much nicer"""
		line = b'PRIVMSG ' + recipient + b' :' + message
		self.serverthread_object.send_line_raw(line)

	def bot_response(self, recipient, message):
		"""Prefix message with ZWSP and convert from unicode to bytestring if necessary."""
		if isinstance(message, str):
			message = message.encode('utf-8')

		self.msg(recipient, '\u200b'.encode('utf-8') + message)

	def nick(self, nick):
		"""Send a NICK command and update the internal nick tracking state"""
		with self.serverthread_object.nick_lock:
			line = b'NICK ' + nick
			self.serverthread_object.send_line_raw(line)
			self.serverthread_object.nick = nick

	def get_nick(self):
		"""Returns current nick"""
		with self.serverthread_object.nick_lock:
			return self.serverthread_object.nick

	def set_nick(self, nick):
		"""Set the internal nick tracking state"""
		with self.serverthread_object.nick_lock:
			self.serverthread_object.nick = nick

	def join(self, channel):
		"""Send a JOIN command and update the internal channel tracking state"""
		with self.serverthread_object.channels_lock:
			line = b'JOIN ' + channel
			self.serverthread_object.send_line_raw(line)
			self.serverthread_object.channels.add(channel)

	def part(self, channel, message = b''):
		"""Send a PART command and update the internal channel tracking state"""
		with self.serverthread_object.channels_lock:
			line = b'PART %s :%s' % (channel, message)
			self.serverthread_object.send_line_raw(line)
			self.serverthread_object.channels.removeadd(channel)

	def get_channels(self):
		"""Returns the current set of channels"""
		with self.serverthread_object.channels_lock:
			return self.serverthread_object.channels

	def set_channels(self, channels):
		"""Set the current set of channels variable"""
		with self.serverthread_object.channels_lock:
			self.serverthread_object.channels = channels

	def quit(self):
		self.serverthread_object.control_channel.send((controlmessage_types.quit,))
		self.serverthread_object.logging_channel.send((logmessage_types.internal, internal_submessage_types.quit))
		cron.quit(self.cron)

	def log(self, *args, **kwargs):
		"""Log a status message. Supports normal print() arguments."""
		self.serverthread_object.logging_channel.send((logmessage_types.status, args, kwargs))

	def error(self, message):
		"""Log an error"""
		self.serverthread_object.logging_channel.send((logmessage_types.internal, internal_submessage_types.error, message))


# ServerThread(server, control_channel, cron_control_channel, logging_channel)
# Creates a new server main loop thread
class ServerThread(threading.Thread):
	def __init__(self, server, control_channel, cron_control_channel, logging_channel):
		self.server = server
		self.control_channel = control_channel
		self.cron_control_channel = cron_control_channel
		self.logging_channel = logging_channel

		self.server_socket_write_lock = threading.Lock()

		self.last_send = 0
		self.last_send_lock = threading.Lock()

		self.nick = None
		self.nick_lock = threading.Lock()

		self.channels = set()
		self.channels_lock = threading.Lock()

		threading.Thread.__init__(self)

	def send_line_raw(self, line):
		# Sanitize line just in case
		line = line.replace(b'\r', b'').replace(b'\n', b'')[:510]

		with self.last_send_lock:
			now = time.monotonic()
			if now - self.last_send < 1:
				# Schedule our message sending one second after the last one
				self.last_send += 1
				wait = self.last_send - now

			else:
				self.last_send = now
				wait = 0

		if wait > 0:
			time.sleep(wait)

		with self.server_socket_write_lock:
			self.server_socket.sendall(line + b'\r\n')

		# Don't log PINGs or PONGs
		if not (len(line) >= 5 and (line[:5] == b'PING ' or line[:5] == b'PONG ')):
			self.logging_channel.send((logmessage_types.sent, line.decode(encoding = 'utf-8', errors = 'replace')))

	def handle_line(self, line):
		command, _, arguments = line.partition(b' ')
		split = line.split(b' ')
		if len(split) >= 1 and split[0].upper() == b'PING':
			self.send_line_raw(b'PONG ' + arguments)
		elif len(split) >= 2 and split[0][0:1] == b':' and split[1].upper() == b'PONG':
			# No need to do anything special for PONGs
			pass
		else:
			self.logging_channel.send((logmessage_types.received, line.decode(encoding = 'utf-8', errors = 'replace')))
			# Ensure we have a bytestring, because bytearray can be annoying to deal with
			line = bytes(line)
			line_handling.handle_line(line, irc = self.api)

	def mainloop(self):
		# Register both the server socket and the control channel to a polling object
		poll = select.poll()
		poll.register(self.server_socket, select.POLLIN)
		poll.register(self.control_channel, select.POLLIN)

		# Keep buffer for input
		server_input_buffer = bytearray()

		quitting = False
		reconnecting = False
		while not quitting and not reconnecting:
			# Wait until we can do something
			for fd, event in poll.poll():
				# Server
				if fd == self.server_socket.fileno():
					# Ready to receive, read into buffer and handle full messages
					if event | select.POLLIN:
						data = self.server_socket.recv(1024)

						# Mo data to be read even as POLLIN triggered → connection has broken
						# Log it and try reconnecting
						if data == b'':
							self.logging_channel.send((logmessage_types.internal, internal_submessage_types.error, 'Empty read'))
							reconnecting = True
							break

						server_input_buffer.extend(data)

						# Try to see if we have a full line ending with \r\n in the buffer
						# If yes, handle it
						while b'\r\n' in server_input_buffer:
							# Newline was found, split buffer
							line, _, server_input_buffer = server_input_buffer.partition(b'\r\n')

							self.handle_line(line)

						# Remove possible pending ping timeout timer and reset ping timer to 3 minutes
						cron.delete(self.cron_control_channel, self.control_channel, (controlmessage_types.ping_timeout,))
						cron.reschedule(self.cron_control_channel, 3 * 60, self.control_channel, (controlmessage_types.ping,))

					else:
						error_message = 'Event on server socket: %s' % event
						self.logging_channel.send((logmessage_types.internal, internal_submessage_types.error, error_message))

				# Control
				elif fd == self.control_channel.fileno():
					command_type, *arguments = self.control_channel.recv()
					if command_type == controlmessage_types.quit:
						quitting = True

					elif command_type == controlmessage_types.send_line:
						assert len(arguments) == 1
						irc_command, space, arguments = arguments[0].encode('utf-8').partition(b' ')
						line = irc_command.upper() + space + arguments
						self.send_line_raw(line)

					elif command_type == controlmessage_types.ping:
						assert len(arguments) == 0
						self.send_line_raw(b'PING :foo')
						# Reset ping timeout timer to 2 minutes
						cron.reschedule(self.cron_control_channel, 2 * 60, self.control_channel, (controlmessage_types.ping_timeout,))

					elif command_type == controlmessage_types.ping_timeout:
						self.logging_channel.send((logmessage_types.internal, internal_submessage_types.error, 'Ping timeout'))
						reconnecting = True

					elif command_type == controlmessage_types.reconnect:
						reconnecting = True

					else:
						error_message = 'Unknown control message: %s' % repr((command_type, *arguments))
						self.logging_channel.send((logmessage_types.internal, internal_submessage_types.error, error_message))

				else:
					assert False #unreachable

		if reconnecting:
			return True
		else:
			return False

	def run(self):
		while True:
			# Connect to given server
			address = (self.server.host, self.server.port)
			try:
				self.server_socket = socket.create_connection(address)
			except (ConnectionRefusedError, socket.gaierror):
				# Tell controller we failed
				self.logging_channel.send((logmessage_types.internal, internal_submessage_types.error, "Can't connect to %s:%s" % address))

				# Try reconnecting in a minute
				cron.reschedule(self.cron_control_channel, 60, self.control_channel, (controlmessage_types.reconnect,))

				# Handle messages
				reconnect = True
				while True:
					command_type, *arguments = self.control_channel.recv()

					if command_type == controlmessage_types.reconnect:
						break

					elif command_type == controlmessage_types.quit:
						reconnect = False
						break

					else:
						error_message = 'Control message not supported when not connected: %s' % repr((command_type, *arguments))
						self.logging_channel.send((logmessage_types.internal, internal_submessage_types.error, error_message))

				# Remove the reconnect message in case we were told to reconnnect manually
				cron.delete(self.cron_control_channel, self.control_channel, (controlmessage_types.reconnect,))

				if reconnect:
					continue
				else:
					break

			# Create an API object to give to outside line handler
			self.api = API(self)

			try:
				# Run initialization
				self.send_line_raw(b'USER %s a a :%s' % (self.server.username.encode('utf-8'), self.server.realname.encode('utf-8')))

				# Set up nick
				self.api.nick(self.server.nick.encode('utf-8'))

				# Run the on_connect hook, to allow further setup
				botcmd.on_connect(irc = self.api)

				# Join channels
				for channel in self.server.channels:
					self.api.join(channel.encode('utf-8'))

				# Schedule a ping to be sent in 3 minutes of no activity
				cron.reschedule(self.cron_control_channel, 3 * 60, self.control_channel, (controlmessage_types.ping,))

				# Run mainloop
				reconnecting = self.mainloop()

				if not reconnecting:
					# Run bot cleanup code
					botcmd.on_quit(irc = self.api)

					# Tell the server we're quiting
					self.send_line_raw(b'QUIT :%s exiting normally' % self.server.username.encode('utf-8'))
					self.server_socket.close()

					break

				else:
					# Tell server we're reconnecting
					self.send_line_raw(b'QUIT :Reconnecting')
					self.server_socket.close()

			except (BrokenPipeError, TimeoutError) as err:
				# Connection broke, log it and try to reconnect
				self.logging_channel.send((logmessage_types.internal, internal_submessage_types.error, 'Broken socket/pipe or timeout'))
				self.server_socket.close()

		# Tell controller we're quiting
		self.logging_channel.send((logmessage_types.internal, internal_submessage_types.quit))

		# Tell cron we're quiting
		cron.quit(cron_control_channel)

# spawn_serverthread(server, cron_control_channel, logging_channel) → control_channel
# Creates a ServerThread for given server and returns the channel for controlling it
def spawn_serverthread(server, cron_control_channel, logging_channel):
	thread_control_socket, spawner_control_socket = socket.socketpair()
	control_channel = channel.Channel()
	ServerThread(server, control_channel, cron_control_channel, logging_channel).start()
	return control_channel

# spawn_loggerthread() → logging_channel, dead_notify_channel
# Spawn logger thread and returns the channel it logs and the channel it uses to notify about quiting
def spawn_loggerthread():
	logging_channel = channel.Channel()
	dead_notify_channel = channel.Channel()
	LoggerThread(logging_channel, dead_notify_channel).start()
	return logging_channel, dead_notify_channel

# read_config() → config, server
# Reads the configuration file and returns the configuration object as well as a server object for spawn_serverthread
def read_config():
	config = configparser.ConfigParser()
	config.read('bot.conf')

	host = config['server']['host']
	port = int(config['server']['port'])
	nick = config['server']['nick']
	username = config['server']['username']
	realname = config['server']['realname']
	channels = config['server']['channels'].split()

	server = Server(host = host, port = port, nick = nick, username = username, realname = realname, channels = channels)

	return config, server

if __name__ == '__main__':
	config, server = read_config()

	botcmd.initialize(config = config)

	cron_control_channel = cron.start()
	logging_channel, dead_notify_channel = spawn_loggerthread()
	control_channel = spawn_serverthread(server, cron_control_channel, logging_channel)

	while True:
		message = dead_notify_channel.recv(blocking = False)
		if message is not None:
			if message[0] == controlmessage_types.quit:
				break

		cmd = input('')
		if cmd == 'q':
			print('Keyboard quit')
			control_channel.send((controlmessage_types.quit,))
			logging_channel.send((logmessage_types.internal, internal_submessage_types.quit))
			cron.quit(cron_control_channel)
			break

		elif cmd == 'r':
			print('Keyboard reconnect')
			control_channel.send((controlmessage_types.reconnect,))

		elif len(cmd) > 0 and cmd[0] == '/':
			control_channel.send((controlmessage_types.send_line, cmd[1:]))
