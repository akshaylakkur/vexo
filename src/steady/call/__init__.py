"""Phoning the caregiver: Twilio carries the call, Pipecat runs it, Deepgram listens and speaks.

    caller.py   who to call, in what order, and the `tasks` row for each alert
    server.py   the call server (TwiML, status callbacks, audio socket) and its public tunnel
    bot.py      one call as a Pipecat pipeline
    twilio.py   Twilio's REST API and TwiML
"""
