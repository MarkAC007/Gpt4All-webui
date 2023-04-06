from flask import Flask, jsonify, request, render_template, Response, stream_with_context
from nomic.gpt4all import GPT4All
import argparse
import threading
from io import StringIO
import sys
import re
import sqlite3
from datetime import datetime

import sqlite3
import json
import time 
import traceback

import select

#=================================== Database ==================================================================
db_path = 'database.db'
class Discussion:
    def __init__(self, discussion_id, db_path='database.db'):
        self.discussion_id = discussion_id
        self.db_path = db_path

    @staticmethod
    def create_discussion(db_path='database.db', title='untitled'):
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO discussion (title) VALUES (?)", (title,))
            discussion_id = cur.lastrowid
            conn.commit()
        return Discussion(discussion_id, db_path)

    def add_message(self, sender, content):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute('INSERT INTO message (sender, content, discussion_id) VALUES (?, ?, ?)',
                         (sender, content, self.discussion_id))
            conn.commit()

    def get_messages(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.cursor().execute('SELECT * FROM message WHERE discussion_id=?', (self.discussion_id,))
        return [{'sender': row[1], 'content': row[2]} for row in conn.cursor().fetchall()]

    def remove_discussion(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.cursor().execute('DELETE FROM discussion WHERE id=?', (self.discussion_id,))
            conn.commit()

def last_discussion_has_messages(db_path='database.db'):
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM message ORDER BY id DESC LIMIT 1")
        last_message = c.fetchone()
    return last_message is not None

def export_to_json(db_path='database.db'):
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute('SELECT * FROM discussion')
        discussions = []
        for row in cur.fetchall():
            discussion_id = row[0]
            discussion = {'id': discussion_id, 'messages': []}
            cur.execute('SELECT * FROM message WHERE discussion_id=?', (discussion_id,))
            for message_row in cur.fetchall():
                discussion['messages'].append({'sender': message_row[1], 'content': message_row[2]})
            discussions.append(discussion)
        return discussions

def remove_discussions(db_path='database.db'):
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute('DELETE FROM message')
        cur.execute('DELETE FROM discussion')
        conn.commit()

# create database schema
print("Checking discussions database...",end="")
with sqlite3.connect(db_path) as conn:
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE  IF NOT EXISTS discussion (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE  IF NOT EXISTS message (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT NOT NULL,
        content TEXT NOT NULL,
        discussion_id INTEGER NOT NULL,
        FOREIGN KEY (discussion_id) REFERENCES discussion(id)
    )
    ''')
    conn.commit()
print("Ok")
# ========================================================================================================================



app = Flask("GPT4All-WebUI")
class Gpt4AllWebUI():
    def __init__(self, chatbot_bindings, app, db_path='database.db') -> None:
        self.current_discussion = None
        self.chatbot_bindings = chatbot_bindings
        self.app=app
        self.db_path= db_path
        self.add_endpoint('/', '', self.index, methods=['GET'])
        self.add_endpoint('/stream', 'stream', self.stream, methods=['GET'])
        self.add_endpoint('/new-discussion', 'new-discussion', self.new_discussion, methods=['POST'])
        self.add_endpoint('/export', 'export', self.export, methods=['GET'])
        self.add_endpoint('/new_discussion', 'new_discussion', self.new_discussion, methods=['GET'])
        self.add_endpoint('/bot', 'bot', self.bot, methods=['POST'])
        # Chatbot conditionning
        # response = self.chatbot_bindings.prompt("This is a discussion between A user and an AI. AI responds to user questions in a helpful manner. AI is not allowed to lie or deceive. AI welcomes the user\n### Response:")
        # print(response)

    def add_endpoint(self, endpoint=None, endpoint_name=None, handler=None, methods=['GET'], *args, **kwargs):
        self.app.add_url_rule(endpoint, endpoint_name, handler, methods=methods, *args, **kwargs)

    def index(self):
        return render_template('chat.html')

    def format_message(self, message):
        # Look for a code block within the message
        pattern = re.compile(r"(```.*?```)", re.DOTALL)
        match = pattern.search(message)

        # If a code block is found, replace it with a <code> tag
        if match:
            code_block = match.group(1)
            message = message.replace(code_block, f"<code>{code_block[3:-3]}</code>")

        # Return the formatted message
        return message


    def stream(self):
        def generate():
            # Replace this with your text-generating code
            for i in range(10):
                yield f'This is line {i+1}\n'
                time.sleep(1)

        return Response(stream_with_context(generate()))

    def new_discussion(self):        
        tite = request.args.get('tite')
        self.current_discussion= Discussion.create_discussion(db_path, tite)
        # Get the current timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Insert a new discussion into the database
        conn.cursor().execute("INSERT INTO discussions (created_at) VALUES (?)", (timestamp,))
        conn.commit()

        # Return a success response
        return json.dumps({'id': self.current_discussion.discussion_id})

    def export(self):
        return jsonify(export_to_json(self.db_path))


    @stream_with_context
    def parse_to_prompt_stream(self):
        bot_says = ['']
        point = b''
        bot = self.chatbot_bindings.bot
        self.stop=False
        wait_val = 15.0 # At the beginning the server may need time to send data. we wait 15s
        while not self.stop:
            readable, _, _ = select.select([bot.stdout], [], [], wait_val)
            if bot.stdout in readable:
                point += bot.stdout.read(1)
                try:
                    character = point.decode("utf-8")
                    wait_val=1.0 # Reduce the wait duration to 1s
                    # if character == "\f": # We've replaced the delimiter character with this.
                    #    return "\n".join(bot_says)
                    if character == "\n":
                        bot_says.append('\n')
                        yield '\n'
                    else:
                        bot_says[-1] += character
                        yield character
                    point = b''

                except UnicodeDecodeError:
                    if len(point) > 4:
                        point = b''
            else:
                return "\n".join(bot_says)
    def bot(self):
        self.stop=True
        with sqlite3.connect('database.db') as conn:
            try:
                if self.current_discussion is None or not last_discussion_has_messages():
                    self.current_discussion=Discussion.create_discussion(self.db_path)

                self.current_discussion.add_message("user", request.json['message'])    
                message = f"{request.json['message']}"
                print(f"Received message : {message}")
                bot = self.chatbot_bindings.bot
                bot.stdin.write(message.encode('utf-8'))
                bot.stdin.write(b"\n")
                bot.stdin.flush()

                # Segmented (the user receives the output as it comes)
                return Response(stream_with_context(self.parse_to_prompt_stream()))
            
                # One shot response (the user should wait for the message to apear at once.)
                #response = format_message(self.chatbot_bindings.prompt(message, write_to_stdout=True).lstrip('# '))
                #return jsonify(response)
            except Exception as ex:
                print(ex)
                msg = traceback.print_exc()
                return "<b style='color:red;'>Exception :<b>"+str(ex)+"<br>"+traceback.format_exc()+"<br>Please report exception"
    def new_discussion(self):
        self.chatbot_bindings.close()
        self.chatbot_bindings.open()
        print("chatbot reset successfully")
        return "chatbot reset successfully"

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Start the chatbot Flask app.')

    parser.add_argument('--temp', type=float, default=0.1, help='Temperature parameter for the model.')
    parser.add_argument('--n_predict', type=int, default=128, help='Number of tokens to predict at each step.')
    parser.add_argument('--top_k', type=int, default=40, help='Value for the top-k sampling.')
    parser.add_argument('--top_p', type=float, default=0.95, help='Value for the top-p sampling.')
    parser.add_argument('--repeat_penalty', type=float, default=1.3, help='Penalty for repeated tokens.')
    parser.add_argument('--repeat_last_n', type=int, default=64, help='Number of previous tokens to consider for the repeat penalty.')
    parser.add_argument('--ctx_size', type=int, default=2048, help='Size of the context window for the model.')
    parser.add_argument('--debug', dest='debug', action='store_true', help='launch Flask server in debug mode')
    parser.add_argument('--host', type=str, default='172.31.159.124', help='the hostname to listen on')
    parser.add_argument('--port', type=int, default=9600, help='the port to listen on')
    parser.set_defaults(debug=False)

    args = parser.parse_args()

    chatbot_bindings = GPT4All(decoder_config = {
                'temp': args.temp,
                'n_predict':args.n_predict,
                'top_k':args.top_k,
                'top_p':args.top_p,
                #'color': True,#"## Instruction",
                'repeat_penalty': args.repeat_penalty,
                'repeat_last_n':args.repeat_last_n,
                'ctx_size': args.ctx_size
            })
    chatbot_bindings.open()  
    bot = Gpt4AllWebUI(chatbot_bindings, app, db_path)  

    if args.debug:
        app.run(debug=True, host=args.host, port=args.port)
    else:
        app.run(host=args.host, port=args.port)
