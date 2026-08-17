"""Readwise API interaction module with upsert and reconcile support.

This is a patched variant used by the automation wrapper. It extends the
original Readwise class by adding:
- per-highlight upsert logic that uses a SQLite LocalState to persist
  mappings from Zotero keys to Readwise highlight ids
- reconcile mode which tries to discover existing Readwise highlights that
  include a machine tag like `.zotero:ABC123` in the note field before creating
  new highlights

This file is intended to be copied over the upstream readwise.py in the
Zotero2Readwise checkout by patches/apply_patches.sh during CI.
"""

import time
from dataclasses import dataclass
from enum import Enum
from json import dump

import requests

from zotero2readwise import FAILED_ITEMS_DIR
from zotero2readwise.exception import Zotero2ReadwiseError
from zotero2readwise.helper import sanitize_tag
from zotero2readwise.zotero import ZoteroItem

# Local helpers from our patches
try:
    # When used via apply_patches, helper_hash and state will be in package
    from zotero2readwise.state import LocalState
    from zotero2readwise.helper_hash import content_hash
except Exception:
    # Fallback — these imports will fail when running upstream directly; they
    # are only expected to be available when our apply_patches script copied
    # patched modules into the Zotero2Readwise package.
    LocalState = None  # type: ignore
    def content_hash(*parts):  # type: ignore
        return ""


@dataclass
class ReadwiseAPI:
    base_url: str = "https://readwise.io/api/v2"
    highlights: str = base_url + "/highlights/"
    books: str = base_url + "/books/"


class Category(Enum):
    articles = 1
    books = 2
    tweets = 3
    podcasts = 4


@dataclass
class ReadwiseHighlight:
    text: str
    title: str | None = None
    author: str | None = None
    image_url: str | None = None
    source_url: str | None = None
    source_type: str | None = None
    category: str | None = None
    note: str | None = None
    location: int | None = 0
    location_type: str | None = "page"
    highlighted_at: str | None = None
    highlight_url: str | None = None

    def __post_init__(self):
        if not self.location:
            self.location = None

    def get_nonempty_params(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


class Readwise:
    def __init__(self, readwise_token: str, custom_tag: str | None = None):
        self._token = readwise_token
        self._header = {"Authorization": f"Token {self._token}"}
        self.endpoints = ReadwiseAPI
        self.failed_highlights: list = []
        self.custom_tag = custom_tag
        # State store can be provided later; default created on demand
        self._state: LocalState | None = None

    def _ensure_state(self, db_path: str | None = None) -> LocalState | None:
        if LocalState is None:
            return None
        if self._state is None:
            self._state = LocalState(db_path)
        return self._state

    def create_highlights(
        self,
        highlights: list[dict],
        batch_size: int = 500,
        batch_delay: float = 0.5,
        max_retries: int = 3,
        retry_delay: float = 10.0,
    ) -> dict | None:
        total = len(highlights)
        num_batches = -(-total // batch_size)

        last_result = None
        for batch_num, i in enumerate(range(0, total, batch_size), start=1):
            batch = highlights[i : i + batch_size]
            print(
                f"  Uploading batch {batch_num}/{num_batches} "
                f"({i + 1}–{min(i + batch_size, total)} of {total})...",
                flush=True,
            )

            for attempt in range(1, max_retries + 1):
                resp = requests.post(
                    url=self.endpoints.highlights,
                    headers=self._header,
                    json={"highlights": batch},
                )

                if resp.status_code == 200:
                    try:
                        last_result = resp.json()
                    except ValueError:
                        last_result = None
                    break

                if resp.status_code in (429, 502, 503) and attempt < max_retries:
                    if resp.status_code == 429:
                        wait = int(resp.headers.get("Retry-After", retry_delay * attempt))
                    else:
                        wait = retry_delay * attempt
                    print(
                        f"  Batch {batch_num} failed with {resp.status_code} "
                        f"(attempt {attempt}/{max_retries}). Retrying in {wait:.0f}s..."
                    )
                    time.sleep(wait)
                    continue

                # Non-retryable or out of retries
                error_log_file = (
                    f"error_log_{resp.status_code}_failed_post_request_to_readwise.json"
                )
                try:
                    error_content = resp.json() if resp.text.strip() else {"error": "Empty response body"}
                except ValueError:
                    error_content = {"error": "Invalid JSON response", "raw_response": resp.text[:500]}
                with open(error_log_file, "w", encoding="utf-8") as f:
                    dump(error_content, f, indent=4, ensure_ascii=False)
                raise Zotero2ReadwiseError(
                    f"Uploading to Readwise failed with following details:\n"
                    f"POST request Status Code={resp.status_code} ({resp.reason})\n"
                    f"Error log is saved to {error_log_file} file."
                )

            if batch_num < num_batches:
                time.sleep(batch_delay)

        return last_result

    @staticmethod
    def convert_tags_to_readwise_format(tags: list[str] | None) -> str:
        if not tags:
            return ""
        return " ".join([f".{sanitize_tag(t.lower())}" for t in tags])

    def format_readwise_note(self, tags: list[str] | None, comment: str | None, zotero_key: str | None = None) -> str | None:
        rw_tags = self.convert_tags_to_readwise_format(tags)
        highlight_note = ""
        if self.custom_tag:
            highlight_note += f".{sanitize_tag(self.custom_tag.lower())} "
        if rw_tags:
            highlight_note += rw_tags + "\n"
        elif self.custom_tag:
            highlight_note = highlight_note.rstrip() + "\n"
        if comment:
            highlight_note += comment
        # Append machine tag for reconciliation and future lookups
        if zotero_key:
            # Keep it unobtrusive but searchable
            highlight_note = (highlight_note or "") + ("\n" if highlight_note else "") + f".zotero:{zotero_key}"
        return highlight_note if highlight_note else None

    def convert_zotero_annotation_to_readwise_highlight(self, annot: ZoteroItem, include_zotero_key: bool = True) -> ReadwiseHighlight:
        highlight_note = self.format_readwise_note(tags=annot.tags, comment=annot.comment, zotero_key=(annot.key if include_zotero_key else None))
        if annot.page_label and annot.page_label.isnumeric():
            location = int(annot.page_label)
        else:
            location = 0
        highlight_url = None
        if annot.attachment_url is not None:
            attachment_id = annot.attachment_url.split("/")[-1]
            annot_id = annot.annotation_url.split("/")[-1]
            highlight_url = f"zotero://open-pdf/library/items/{attachment_id}?page={location}%&annotation={annot_id}"
        return ReadwiseHighlight(
            text=annot.text,
            title=annot.title,
            note=highlight_note,
            author=annot.creators,
            category=(Category.articles.name if annot.document_type != "book" else Category.books.name),
            highlighted_at=annot.annotated_at,
            source_url=annot.source_url,
            highlight_url=(annot.annotation_url if highlight_url is None else highlight_url),
            location=location,
        )

    def update_highlight(self, highlight_id: str | int, payload: dict) -> requests.Response:
        url = f"{self.endpoints.highlights}{highlight_id}/"
        return requests.patch(url, headers=self._header, json=payload)

    def delete_highlight(self, highlight_id: str | int) -> requests.Response:
        url = f"{self.endpoints.highlights}{highlight_id}/"
        return requests.delete(url, headers=self._header)

    def find_readwise_highlight_by_zotero_key(self, zotero_key: str, max_pages: int = 5) -> str | None:
        """Attempt to locate an existing Readwise highlight that contains the machine
        tag `.zotero:{zotero_key}` in its note.

        This pages through Readwise highlights (recent first) up to max_pages.
        Returns the highlight id if found, otherwise None.
        """
        tag = f".zotero:{zotero_key}"
        for page in range(1, max_pages + 1):
            resp = requests.get(self.endpoints.highlights, headers=self._header, params={"page": page})
            if resp.status_code != 200:
                # On rate limit or other issues, abort reconcile
                print(f"Warning: reconcile GET /highlights failed with {resp.status_code}")
                return None
            try:
                data = resp.json()
            except ValueError:
                return None
            items = data.get("results") or data.get("highlights") or data
            # items may be a list or dict; handle common shapes
            if isinstance(items, dict):
                # some APIs return {results: [...], next: ...}
                items = items.get("results", [])
            for h in items:
                note = h.get("note") or ""
                if tag in note:
                    return str(h.get("id") or h.get("highlight_id") or h.get("pk"))
        return None

    def upsert_single_highlight(self, annot: ZoteroItem, state_db_path: str | None = None, reconcile: bool = False, max_reconcile_pages: int = 5) -> None:
        """Upsert a single ZoteroItem into Readwise using LocalState.

        Steps:
        1. Compute content hash.
        2. Check LocalState for existing mapping.
        3. If mapping missing and reconcile=True, try to find an existing Readwise highlight with machine tag.
        4. If mapping exists and hash/version unchanged: skip.
        5. If mapping exists and changed: attempt PATCH; on 404 or failure fallback to DELETE+CREATE.
        6. If mapping missing: CREATE and store mapping.
        """
        state = self._ensure_state(state_db_path)
        key = annot.key
        current_hash = content_hash(annot.text, annot.comment or "", annot.page_label or "", annot.color or "")
        rec = state.get(key) if state else None

        payload = self.convert_zotero_annotation_to_readwise_highlight(annot).get_nonempty_params()

        # If no existing mapping and reconcile requested, try to find an existing highlight
        if not rec and reconcile:
            found_id = self.find_readwise_highlight_by_zotero_key(key, max_pages=max_reconcile_pages)
            if found_id:
                print(f"Reconcile: adopting existing Readwise highlight {found_id} for Zotero {key}")
                rec = {"zotero_key": key, "readwise_id": found_id}

        if rec is None:
            # Create new highlight (single-item create to be able to read returned id)
            resp = requests.post(url=self.endpoints.highlights, headers=self._header, json={"highlights": [payload]})
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                # Attempt to parse created id from response. The Readwise API v2 response
                # shape can vary; try common patterns.
                new_id = None
                if isinstance(data, dict):
                    # If API returns {"highlights": [{"id": 123, ...}]}
                    if "highlights" in data and isinstance(data["highlights"], list) and data["highlights"]:
                        new_id = data["highlights"][0].get("id")
                    # else try results
                    elif "results" in data and isinstance(data["results"], list) and data["results"]:
                        new_id = data["results"][0].get("id")
                # fallback: Readwise sometimes returns nothing useful; leave new_id None
                state_rec = {"readwise_id": str(new_id) if new_id is not None else None, "zotero_version": annot.version, "content_hash": current_hash, "last_synced_at": time.time()}
                if state:
                    state.set(key, state_rec)
                print(f"Created Readwise highlight for Zotero {key} (id={new_id})")
            else:
                print(f"Failed to create highlight for {key}: status {resp.status_code}")
                failed_item = annot.get_nonempty_params()
                failed_item["error_type"] = "CreateFailed"
                failed_item["error_message"] = f"Status {resp.status_code}"
                self.failed_highlights.append(failed_item)
            return

        # rec exists
        rw_id = rec.get("readwise_id")
        if rec.get("content_hash") == current_hash and rec.get("zotero_version") == annot.version:
            # Nothing changed
            return

        # Attempt update in-place
        if rw_id:
            update_resp = self.update_highlight(rw_id, payload)
            if update_resp.status_code in (200, 204):
                if state:
                    state.set(key, {"readwise_id": rw_id, "zotero_version": annot.version, "content_hash": current_hash, "last_synced_at": time.time()})
                print(f"Updated Readwise highlight {rw_id} for Zotero {key}")
                return
            elif update_resp.status_code == 404:
                # Remote highlight missing; delete local mapping and recreate
                print(f"Readwise highlight {rw_id} not found (404). Recreating...")
                if state:
                    state.delete(key)
                # Try create flow
                resp = requests.post(url=self.endpoints.highlights, headers=self._header, json={"highlights": [payload]})
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = {}
                    new_id = None
                    if isinstance(data, dict):
                        if "highlights" in data and isinstance(data["highlights"], list) and data["highlights"]:
                            new_id = data["highlights"][0].get("id")
                    if state:
                        state.set(key, {"readwise_id": str(new_id) if new_id is not None else None, "zotero_version": annot.version, "content_hash": current_hash, "last_synced_at": time.time()})
                    print(f"Re-created Readwise highlight for Zotero {key} (id={new_id})")
                    return
                else:
                    print(f"Failed to re-create highlight for {key}: status {resp.status_code}")
                    failed_item = annot.get_nonempty_params()
                    failed_item["error_type"] = "RecreateFailed"
                    failed_item["error_message"] = f"Status {resp.status_code}"
                    self.failed_highlights.append(failed_item)
                    return
            else:
                # Other error: try delete+create fallback
                print(f"Update failed for {rw_id} (status {update_resp.status_code}). Trying delete+create fallback.")
                try:
                    del_resp = self.delete_highlight(rw_id)
                except Exception:
                    del_resp = None
                # Attempt to create new
                resp = requests.post(url=self.endpoints.highlights, headers=self._header, json={"highlights": [payload]})
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = {}
                    new_id = None
                    if isinstance(data, dict):
                        if "highlights" in data and isinstance(data["highlights"], list) and data["highlights"]:
                            new_id = data["highlights"][0].get("id")
                    if state:
                        state.set(key, {"readwise_id": str(new_id) if new_id is not None else None, "zotero_version": annot.version, "content_hash": current_hash, "last_synced_at": time.time()})
                    print(f"Delete+create succeeded for Zotero {key} (new id={new_id})")
                    return
                else:
                    print(f"Delete+create failed for {key}: status {resp.status_code}")
                    failed_item = annot.get_nonempty_params()
                    failed_item["error_type"] = "DeleteCreateFailed"
                    failed_item["error_message"] = f"Status {resp.status_code}"
                    self.failed_highlights.append(failed_item)
                    return
        else:
            # No readwise_id in mapping — treat like create
            resp = requests.post(url=self.endpoints.highlights, headers=self._header, json={"highlights": [payload]})
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                new_id = None
                if isinstance(data, dict):
                    if "highlights" in data and isinstance(data["highlights"], list) and data["highlights"]:
                        new_id = data["highlights"][0].get("id")
                if state:
                    state.set(key, {"readwise_id": str(new_id) if new_id is not None else None, "zotero_version": annot.version, "content_hash": current_hash, "last_synced_at": time.time()})
                print(f"Created Readwise highlight for Zotero {key} (id={new_id})")
            else:
                print(f"Failed to create highlight for {key}: status {resp.status_code}")
                failed_item = annot.get_nonempty_params()
                failed_item["error_type"] = "CreateFailed"
                failed_item["error_message"] = f"Status {resp.status_code}"
                self.failed_highlights.append(failed_item)
            return

    def post_zotero_annotations_to_readwise(
        self,
        zotero_annotations: list[ZoteroItem],
        recent_first: bool = True,
        batch_size: int = 500,
        state_db_path: str | None = None,
        reconcile: bool = False,
        max_reconcile_pages: int = 5,
    ) -> None:
        # reuse original sorting logic
        if recent_first:
            doc_latest: dict[str, str] = {}
            for a in zotero_annotations:
                title = a.title or ""
                ts = a.annotated_at or ""
                if title not in doc_latest or ts > doc_latest[title]:
                    doc_latest[title] = ts
            zotero_annotations = sorted(zotero_annotations, key=lambda a: a.sort_index or "")
            zotero_annotations = sorted(zotero_annotations, key=lambda a: doc_latest.get(a.title or "", ""), reverse=True)

        print(
            f"\nReadwise: Push {len(zotero_annotations)} Zotero annotations/notes to Readwise...\n"
            f"It may take some time depending on the number of highlights...\n"
            f"A complete message will show up once it's done!\n"
        )

        # For robust upsert + reconcile behavior we operate per-item (slower but safe).
        for annot in zotero_annotations:
            try:
                if len(annot.text) >= 8191:
                    print(
                        f"A Zotero annotation from an item with {annot.title} (item_key={annot.key} and "
                        f"version={annot.version}) cannot be uploaded since the highlight/note is very long. "
                        f"A Readwise highlight can be up to 8191 characters."
                    )
                    failed_item = annot.get_nonempty_params()
                    failed_item["error_type"] = "CharacterLimitExceeded"
                    failed_item["error_message"] = (
                        f"Highlight exceeds 8191 character limit ({len(annot.text)} chars)"
                    )
                    self.failed_highlights.append(failed_item)
                    continue
                # Upsert with state and optional reconcile
                self.upsert_single_highlight(annot, state_db_path=state_db_path, reconcile=reconcile, max_reconcile_pages=max_reconcile_pages)
            except Exception as e:
                failed_item = annot.get_nonempty_params()
                failed_item["error_type"] = type(e).__name__
                failed_item["error_message"] = str(e)
                self.failed_highlights.append(failed_item)
                print(f"Warning: Failed to upsert item {annot.key}: {type(e).__name__}: {e}")
                continue

        finished_msg = ""
        if self.failed_highlights:
            finished_msg = (f"\nNOTE: {len(self.failed_highlights)} highlights failed to upload to Readwise.\n")
        finished_msg += (f"\n{len(zotero_annotations) - len(self.failed_highlights)} highlights were processed (created/updated) against Readwise.\n\n")
        print(finished_msg)

    def save_failed_items_to_json(self, json_filepath_failed_items: str | None = None) -> None:
        FAILED_ITEMS_DIR.mkdir(parents=True, exist_ok=True)
        if json_filepath_failed_items:
            out_filepath = FAILED_ITEMS_DIR.joinpath(json_filepath_failed_items)
        else:
            out_filepath = FAILED_ITEMS_DIR.joinpath("failed_readwise_items.json")

        with open(out_filepath, "w", encoding="utf-8") as f:
            dump(self.failed_highlights, f, indent=4, ensure_ascii=False)
        print(
            f"{len(self.failed_highlights)} highlights failed to format (hence failed to upload to Readwise).\n"
            f"Detail of failed items are saved into {out_filepath}"
        )
