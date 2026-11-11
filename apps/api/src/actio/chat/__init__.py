"""Conversations: what people and models said to each other, kept as a tree.

- `models`: the tables;
- `parts`: what a message is made of;
- `conversations`: creating, listing, renaming and deleting conversations;
- `tree`: adding messages, and reading one branch of the tree.

Nothing here knows about HTTP: the routes are in `actio.api.conversations`. Of accounts
it knows one thing: a conversation belongs to one, so its table points at theirs.
"""
