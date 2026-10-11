import os

from googleapiclient.discovery import build

from app.gmail.oauth import GmailAuthSettings, get_credentials


def register_watch(settings: GmailAuthSettings):
    credentials = get_credentials(settings)
    service = build("gmail", "v1", credentials=credentials)

    return (
        service.users()
        .watch(userId="me", body={"topicName": os.environ["GMAIL_PUBSUB_TOPIC"]})
        .execute()
    )
