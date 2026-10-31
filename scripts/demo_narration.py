"""The spoken explanation of the demos: the script (plain, conversational language), a neural voice, and the timing of every sentence (for the voice-over and the subtitles).

The voice is Microsoft's neural speech (through the `edge-tts` package). The TEXT of the script, and nothing else, is sent to Microsoft's online speech service to be turned into
audio. The scripts below hold no customer data, no keys and no project names; keep it that way when you edit them. The audio is cached by text, so an unchanged script is not
sent again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path

import edge_tts
import imageio_ffmpeg

VOICE = "en-US-AvaMultilingualNeural"
RATE = "-4%"          # a little slower than the default: easier to follow while something happens on the screen


def firebase_to_sqlserver_script(rows: int) -> dict[int, str]:
    """What is said at each step of scripts/demo_firestore_to_sqlserver.py (step numbers as in its captions)."""
    total, copies = rows + 18, 18
    return {
        1: "Welcome. In this demonstration, customer records that live in a Google Firebase database are migrated through the whole pipeline. They are cleaned, checked for duplicates, "
           "validated, and delivered into a SQL Server table. Everything you see is the real application, working with a real Firebase project and a real SQL Server.",
        2: "First, we add the source. Firebase stores every customer as a document, with nested parts such as the address and the contact details. We give the source a name, "
           "and choose Google Firebase as its type.",
        3: "To connect, we load a service account key file. The browser reads the file, the server keeps the key in memory only, and it is never saved or shown again. "
           "The account only needs permission to read. Now we test the connection, and it works.",
        4: "Next, we pick the collection to bring in. Each collection becomes a table, and the nested parts become dotted columns, like address dot city, and contact dot email.",
        5: "On the summary page, we press Finish. The documents are read, counted against Firebase's own count, and stored as a snapshot. If those numbers did not match, the import would be rejected.",
        6: "This is the profile. For every column it shows how many values are empty, how many are distinct, the lengths, and the most common values, including the nested columns. "
           "It tells us what is really in the data, before anything is changed.",
        7: "Now the destination. We add a new target, and choose SQL Server. The delivery will create a new table here, and fill it with the validated customers.",
        8: "We enter the server and the database, and a login that is allowed to create tables in this one scratch database, and nowhere else. Then we test the connection.",
        9: "Before anything is delivered, the wizard proves that the account can really write. It creates a scratch table, fills it, reads it back, and drops it again. "
           "If this failed, the delivery would be refused.",
        10: "Now the heart of the work, the Mapping Studio. On one side are the columns of the Firebase collection, on the other the SAP business partner fields. "
            "Here we build the mapping by hand. With an AI key, the application can propose it for us.",
        11: "We point this mapping at the SQL Server destination. The destination is chosen together with the mapping, so its own rules can be checked before anybody approves.",
        12: "Now the rules. Each rule says where one SAP field gets its value. Most are simple copies, like the name or the city, and the nested fields are read by their dotted names. "
            "One rule is a short formula, and two are constants.",
        13: "Two fields need lookup tables, the country and the payment terms. The legacy code net sixty becomes the SAP key N T sixty, and cash on delivery becomes cash.",
        14: "Before anyone is asked to approve, we run a dry check on a sample of the real documents. Every sampled customer passes every rule, and the page also shows what the destination has to say.",
        15: "The author of the mapping now submits it for approval. Nothing can run until a second person has approved it.",
        16: "Here we switch to a different person, the steward, who approves the mapping. The system records who approved it, and when. The author can never approve their own mapping.",
        17: f"Now we run the pipeline. The rules are applied to every customer, duplicates are found, and each field is checked against the SAP catalogue. "
            f"The nested e-mail address and the tax number reveal customers that were entered twice. There are {copies} of them.",
        18: "The governance gate is open, because no critical defect is left. But an open gate is not enough. A steward must also sign off the load, as a separate, recorded act. We do that now.",
        19: "Now we deliver. The target is already selected, because we pointed the mapping at it. Show table and SQL displays exactly what will be created, before anything is.",
        20: "We deliver. The table is created and filled in one all-or-nothing step. The rows are counted inside SQL Server, and every single value is compared with what was sent.",
        21: f"Here is the proof, read straight from the database, not from this page. The new table holds {rows} rows, and these are the first of them.",
        22: "We can verify again at any time. Every value in SQL Server is read once more and compared with the validated records. If somebody edited the table afterwards, this check would reveal it.",
        23: f"Finally, the certificate. All {total} records are accounted for: {copies} duplicates, and {rows} delivered. "
            "The banner is red for one reason: the SAP load has not happened, because this run went to SQL Server.",
        24: "That was a Firebase collection, migrated into SQL Server. Nested documents were read, duplicates were merged, a second person approved, the gate and the sign off were enforced, "
            "and every row was counted and compared in the database. The certificate proves it.",
    }


def sqlserver_to_firebase_script(total: int = 1602, valid: int = 1526) -> dict[int, str]:
    """What is said at each step of scripts/demo_sqlserver_to_firestore.py. The numbers are those of its data (the view V_AR_DEMO: 1,602 customers, 1,526 left after duplicates)."""
    copies = total - valid
    return {
        1: "Welcome. In this demonstration, a customer table from an old SQL Server system is migrated through the whole pipeline. It is profiled, mapped to SAP business partner fields, "
           "approved by a second person, checked for duplicates and validated, and then delivered into Google Firebase as documents. Everything you see is the real application, "
           "working with a real SQL Server and a real Firebase project.",
        2: "First, the source. We add the legacy SQL Server database, using a login that can only read, so the application can look at the old data, but can never change it.",
        3: "We enter the server, the database and the login, and test the connection. The wizard connects for real, and counts what the account can see. "
           "The password is kept in the server's memory only.",
        4: f"Now we pick what to bring in. This is a view of {total:,} customers. Each table is landed as a snapshot, and its row count is checked against the database's own count.",
        5: "On the summary page, we press Finish. The rows are read, counted, stored as a snapshot, and profiled. If the count did not match the source, the import would be rejected.",
        6: "This is the profile. For every one of the seventeen columns, it shows the empty values, the distinct values, the lengths and the most common values, "
           "so we know what is really in the table before anything is changed.",
        7: "Now the destination. We add a new target, and choose Google Firebase. Firebase is a document database, so every validated customer will become one document.",
        8: "We load a service account key file from the Google Cloud console. The browser reads it, the server keeps it in memory only, and it is never saved or shown again. "
           "Then we test the connection.",
        9: "Before anything is delivered, the wizard proves that the account can write. It writes one test document, reads it back, and deletes it. If that failed, the delivery would be refused.",
        10: "Now the Mapping Studio. The legacy columns are on one side, the SAP business partner fields on the other. With a Gemini key, the AI proposes the mapping. Here, we build it by hand.",
        11: "We point this mapping at Firebase. Because the destination is chosen together with the mapping, Firebase's own rules, such as keys that can become document identifiers, "
            "and the free plan's quota, are checked before anybody approves.",
        12: "Now the rules. Each one says where an SAP field gets its value. Most are plain copies, like the name, the street and the city. One is a short formula that builds the search term, "
            "and two are constants, such as the business partner group.",
        13: "Two fields need lookup tables, the country and the payment terms. The legacy code net sixty becomes the SAP key N T sixty, and cash on delivery becomes cash.",
        14: "Before anyone approves, we run a dry check on a sample of the real rows. It shows how many pass every rule, and what the destination has to say.",
        15: "The author submits the mapping for approval. Nothing can run until a second person approves it.",
        16: "Now we act as a different person, the steward, and approve. The system records who approved it, and when. The author can never approve their own mapping.",
        17: f"We run the pipeline. The rules are applied to every row, duplicate customers are found, and every field is checked against the SAP catalogue. "
            f"Of {total:,} rows, {copies} turn out to be duplicates.",
        18: "The governance gate is open, because no critical defect is left. A steward must still sign off the load, as a separate, recorded act. We do that now.",
        19: "Now the delivery. The target is already chosen, because the mapping points at it. The preview tells us what Firebase will receive, and checks the collection name, "
            "before anything is written.",
        20: "We deliver. The documents are written, then counted, and every value is compared with what was sent. If anything differed, the documents would be removed again.",
        21: f"Here is the proof, read straight from Google, not through this application. The collection holds {valid:,} documents.",
        22: "We can verify again at any time. Every value in Firebase is read once more, and compared with the validated records. "
            "If somebody edited a document in the Firebase console afterwards, this would show it.",
        23: f"Finally, the certificate. All {total:,} records are accounted for: {copies} duplicates, and {valid:,} delivered. "
            "The banner is red for one reason: the SAP load has not happened, because this run went to Firebase.",
        24: "That was a SQL Server table, migrated into Google Firebase. The source was read without being changed, the mapping was approved by a second person, duplicates were merged, "
            "the gate and the sign off were enforced, and every document was counted and compared in Firebase. The certificate proves it.",
    }


def probe_seconds(path: Path) -> float:
    err = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


async def _synthesize(text: str, voice: str, rate: str, audio: Path, attempts: int = 4) -> list[dict]:
    """The speech service now and then answers a request with no audio at all; the same request works a moment later, so it is simply tried again."""
    for attempt in range(1, attempts + 1):
        cues: list[dict] = []
        try:
            with open(audio, "wb") as fh:
                async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
                    if chunk["type"] == "audio":
                        fh.write(chunk["data"])
                    elif chunk["type"] == "SentenceBoundary":
                        cues.append({"start": chunk["offset"] / 1e7, "end": (chunk["offset"] + chunk["duration"]) / 1e7, "text": chunk["text"]})
            return cues
        except edge_tts.exceptions.NoAudioReceived:
            if attempt == attempts:
                raise
            await asyncio.sleep(2 * attempt)
    return []


def prepare(script: dict[int, str], cache_dir: Path, voice: str = VOICE, rate: str = RATE) -> dict[int, dict]:
    """-> {step: {text, audio (mp3 path), duration (s), cues: [{start, end, text}] relative to the start of the audio}}"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict[int, dict] = {}
    for step, text in script.items():
        key = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:14]
        audio, meta = cache_dir / f"{key}.mp3", cache_dir / f"{key}.json"
        if not (audio.is_file() and meta.is_file()):
            cues = asyncio.run(_synthesize(text, voice, rate, audio))
            meta.write_text(json.dumps(cues), encoding="utf-8")
        out[step] = {"text": text, "audio": str(audio), "duration": probe_seconds(audio), "cues": json.loads(meta.read_text(encoding="utf-8"))}
    return out
