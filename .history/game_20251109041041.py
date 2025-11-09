# (file content starts here)
"""
game.py - Portal Text Adventure with World Editor and in-room Design (upload 1:1 images)

Changes in this version:
- Players can design a new room while standing in any room that currently has only one exit.
  The new room becomes the second exit of the current room (link), turning the experience into a
  "build your own adventure" workflow.
- When designing a room in-place, player can upload a photo (local path or URL). The image is
  processed to a 1:1 square (center-cropped and resized) and saved as images/room_<id>.png.
- The newly created room will contain a back-link (first exit) that points back to the room you
  designed it from, making navigation intuitive. The second exit of the new room defaults to
  existing_or_new unless you configure otherwise in the prompts.
- All manual rooms and images are persisted into rooms.json and images/ respectively.

How to use:
- Run the program, choose Play game.
- If you're in a room that has only one exit, the CLI will accept the command `design` to create
  a new room in-place. You'll be prompted for title, description, coins, and an image path or URL.
  To upload an image from your workstation through a browser, type `upload` when prompted for image.
"""
import os
import json
import random
from pathlib import Path
from typing import Dict, Any, Optional
from rich import print
from rich.prompt import Prompt
from rich.console import Console

# adapter (provides local fallback or HF/Gemini integration)
import ai_adapter as ai

# image processing + upload server
from PIL import Image
from io import BytesIO
import requests
import threading
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
import cgi
import time

DATA_FILE = Path("rooms.json")
IMAGES_DIR = Path("images")
IMAGES_DIR.mkdir(exist_ok=True)
console = Console()

COIN_COST = int(os.environ.get("COIN_COST", "3"))
ADMIN_PASS = os.environ.get("ADMIN_PASS")
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "512x512")
NO_IMAGES = os.environ.get("NO_IMAGES", "") in ("1", "true", "True")

# ----------------------
# Data IO utilities
# ----------------------
def load_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        default = {
            "next_id": 1,
            "total_generated": 0,
            "start_room": 0,
            "rooms": {
                "0": {
                    "id": 0,
                    "title": "The First Chamber",
                    "description": "You awaken in a dim chamber with two shimmering portals. The doorway you came through has vanished. Each portal bears a sign written in an unfamiliar script. The air tastes faintly of iron and rain.",
                    "image": "",
                    "coins": 1,
                    "exits": [
                        {"role": "home_or_death", "label": "Left portal"},
                        {"role": "existing_or_new", "label": "Right portal"}
                    ]
                }
            }
        }
        save_data(default)
        return default
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data: Dict[str, Any]):
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ----------------------
# Image helpers
# ----------------------
def center_crop_square(img: Image.Image) -> Image.Image:
    w, h = img.size
    min_side = min(w, h)
    left = (w - min_side) // 2
    top = (h - min_side) // 2
    return img.crop((left, top, left + min_side, top + min_side))

def process_and_save_image(img_source: str, room_id: int, target_size: int = 512) -> str:
    """
    Accepts local path or URL and writes images/room_<room_id>.png as a 1:1 image.
    Returns path to saved image.
    """
    if img_source.startswith("http://") or img_source.startswith("https://"):
        resp = requests.get(img_source, timeout=30)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGBA")
    else:
        p = Path(img_source).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Image file not found: {img_source}")
        img = Image.open(p).convert("RGBA")

    img_sq = center_crop_square(img)
    img_resized = img_sq.resize((target_size, target_size), Image.LANCZOS)
    save_path = IMAGES_DIR / f"room_{room_id}.png"
    img_resized.save(save_path, format="PNG")
    return str(save_path)

def save_image_bytes_for_room(image_bytes: bytes, room_id: int, target_size: int = 512) -> str:
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    img_sq = center_crop_square(img)
    img_resized = img_sq.resize((target_size, target_size), Image.LANCZOS)
    save_path = IMAGES_DIR / f"room_{room_id}.png"
    img_resized.save(save_path, format="PNG")
    return str(save_path)

# ----------------------
# Simple upload server
# ----------------------
class UploadServer:
    """
    Starts a simple HTTP server that serves an upload form and accepts a file field named 'file'.
    Saves uploaded image processed to 1:1 into images/room_<room_id>.png and sets result_path.
    """

    def __init__(self, room_id: int, host: str = "0.0.0.0"):
        self.room_id = room_id
        self._host = host
        self._port = self._find_free_port()
        self.result_path: Optional[str] = None
        self._event = threading.Event()
        self._server = None
        self._thread = None

    def _find_free_port(self) -> int:
        s = socket.socket()
        s.bind(("", 0))
        addr, port = s.getsockname()
        s.close()
        return port

    def make_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                # quiet logging to console
                return

            def do_GET(self):
                html = f"""
                <!doctype html>
                <html>
                  <head><title>Upload room image</title></head>
                  <body>
                    <h2>Upload image for room {parent.room_id}</h2>
                    <form enctype="multipart/form-data" method="post">
                      <input name="file" type="file" accept="image/*"/><br/><br/>
                      <input type="submit" value="Upload"/>
                    </form>
                    <p>After upload, close this page and return to the game.</p>
                  </body>
                </html>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html.encode("utf-8"))))
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))

            def do_POST(self):
                try:
                    ctype, pdict = cgi.parse_header(self.headers.get("content-type"))
                    if ctype != "multipart/form-data":
                        self.send_response(400)
                        self.end_headers()
                        return
                    fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
                        'REQUEST_METHOD': 'POST',
                        'CONTENT_TYPE': self.headers.get('content-type'),
                    })
                    if "file" not in fs:
                        self.send_response(400)
                        self.end_headers()
                        return
                    fileitem = fs["file"]
                    data = fileitem.file.read()
                    # save, process to 1:1 square
                    saved = save_image_bytes_for_room(data, parent.room_id)
                    parent.result_path = saved
                    parent._event.set()
                    # respond success page
                    html = f"<html><body><h3>Uploaded and saved to {saved}</h3><p>You can close this tab.</p></body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html.encode("utf-8"))))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(f"Error: {e}".encode("utf-8"))
                    parent._event.set()

        return Handler

    def start(self):
        handler = self.make_handler()
        server = HTTPServer((self._host, self._port), handler)
        self._server = server
        def serve():
            try:
                server.serve_forever()
            except Exception:
                pass
        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        return self._port

    def wait_for_upload(self, timeout: int = 180) -> Optional[str]:
        # Wait until file uploaded or timeout
        waited = self._event.wait(timeout)
        # shutdown server
        try:
            if self._server:
                self._server.shutdown()
        except Exception:
            pass
        if waited:
            return self.result_path
        return None

# ----------------------
# Room creation utilities (AI/manual)
# ----------------------
def create_new_room_ai(data: Dict[str, Any], seed: Optional[str] = None) -> Dict[str, Any]:
    room_id = data["next_id"]
    data["next_id"] = room_id + 1

    console.print("[yellow]AI is generating a new room...[/yellow]")
    room_json = ai.generate_room_text(seed)
    data["total_generated"] = data.get("total_generated", 0) + 1

    if data["total_generated"] % 10 == 0:
        room = {
            "id": room_id,
            "title": "Room Creation Computer",
            "description": "A humming floor of glass and chrome displays a console of infinite options. Insert coins to create a custom room, or provide admin credentials to bypass.",
            "image": "",
            "coins": 0,
            "exits": [
                {"role": "home_or_death", "label": "Left Console Gate"},
                {"role": "existing_or_new", "label": "Right Console Gate"}
            ]
        }
        data["rooms"][str(room_id)] = room
        save_data(data)
        return room

    title = room_json.get("title", f"Room {room_id}")
    description = room_json.get("description", "An indescribable place.")
    labels = room_json.get("exit_labels", {})
    exits = [
        {"role": "home_or_death", "label": labels.get("1", "Left")},
        {"role": "existing_or_new", "label": labels.get("2", "Right")}
    ]

    coins = 0
    if random.random() < 0.25:
        coins = random.choice([1, 2])

    room = {
        "id": room_id,
        "title": title,
        "description": description,
        "image": "",
        "coins": coins,
        "exits": exits
    }

    image_prompt = room_json.get("image_prompt", description)
    try:
        if not NO_IMAGES:
            img_bytes = ai.generate_room_image(image_prompt, size=IMAGE_SIZE)
            saved = save_image_bytes_for_room(img_bytes, room_id)
            room["image"] = saved
    except Exception as e:
        console.print(f"[red]Image generation failed: {e}[/red]")
        room["image"] = ""

    data["rooms"][str(room_id)] = room
    save_data(data)
    return room

def create_manual_room(data: Dict[str, Any], manual: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    room_id = data["next_id"]
    data["next_id"] = room_id + 1

    if manual:
        title = manual.get("title", f"Room {room_id}")
        description = manual.get("description", "")
        coins = manual.get("coins", 0)
        exits = manual.get("exits", [
            {"role": "home_or_death", "label": "Left"},
            {"role": "existing_or_new", "label": "Right"}
        ])
        image_path = manual.get("image", "")
    else:
        console.print(f"[cyan]Creating manual room id={room_id}[/cyan]")
        title = Prompt.ask("Title", default=f"Room {room_id}")
        description = Prompt.ask("Description", default="A room created by world editor.")
        coins = int(Prompt.ask("Coins in room (0-5)", default="0"))
        exits = []
        for i in (1, 2):
            console.print(f"Configure exit {i}:")
            role = Prompt.ask("Role (home_or_death / existing_or_new / link)", choices=["home_or_death", "existing_or_new", "link"], default="existing_or_new")
            label = Prompt.ask("Label", default=("Left" if i == 1 else "Right"))
            exit_obj = {"role": role, "label": label}
            if role == "link":
                target = int(Prompt.ask("Target room id (existing room number)", default="0"))
                exit_obj["target"] = target
            exits.append(exit_obj)
        image_path = Prompt.ask("Image file path or URL (leave blank for none)", default="")

    room = {
        "id": room_id,
        "title": title,
        "description": description,
        "image": "",
        "coins": coins,
        "exits": exits
    }

    if image_path:
        try:
            saved = process_and_save_image(image_path, room_id)
            room["image"] = saved
        except Exception as e:
            console.print(f"[red]Image processing failed: {e}[/red]")
            room["image"] = ""

    data["rooms"][str(room_id)] = room
    save_data(data)
    console.print(f"[green]Created manual room {room_id}: {title}[/green]")
    return room

# ----------------------
# Design-from-current-room (in-place) with upload support
# ----------------------
def design_room_from_current(data: Dict[str, Any], current_id: int) -> Dict[str, Any]:
    current_room = data["rooms"][str(current_id)]
    console.print("[bold magenta]Design a new room (this will become the missing exit)[/bold magenta]")
    title = Prompt.ask("Title", default=f"Room {data['next_id']}")
    description = Prompt.ask("Description", default="A room you designed yourself.")
    coins = int(Prompt.ask("Coins in this new room (0-5)", default="0"))
    label_for_current_exit = Prompt.ask("Label for the exit from current room to this new room", default="A new exit")
    img_input = Prompt.ask("Image file path OR URL OR type 'upload' to open a browser uploader (optional)", default="")

    # prepare new room dict with a backlink to current room
    new_room_manual = {
        "title": title,
        "description": description,
        "coins": coins,
        "exits": [
            {"role": "link", "label": f"Back to {current_room.get('title','previous')}", "target": current_id},
            {"role": "existing_or_new", "label": "Right (mysterious)"}
        ]
    }
    # create the new room (gets saved and assigned an id)
    new_room = create_manual_room(data, manual={**new_room_manual, "image": ""})

    # handle image: path/url or upload
    if img_input:
        if img_input.strip().lower() == "upload":
            # start upload server
            uploader = UploadServer(new_room["id"])
            port = uploader.start()
            # present instructions to user
            console.print(f"[blue]Upload server running on port {port}. Open the forwarded port in your browser and upload an image file.[/blue]")
            console.print("[dim]In GitHub Codespaces: open the Ports panel, click the port number and 'Open in Browser'.[/dim]")
            console.print("[dim]Waiting for upload (timeout 180s)...[/dim]")
            saved = uploader.wait_for_upload(timeout=180)
            if saved:
                new_room["image"] = saved
                data["rooms"][str(new_room["id"])] = new_room
                save_data(data)
                console.print(f"[green]Image uploaded and saved to {saved}[/green]")
            else:
                console.print("[red]No image uploaded (timeout or error).[/red]")
        else:
            # treat as path or URL
            try:
                saved = process_and_save_image(img_input, new_room["id"])
                new_room["image"] = saved
                data["rooms"][str(new_room["id"])] = new_room
                save_data(data)
            except Exception as e:
                console.print(f"[red]Failed saving uploaded image: {e}[/red]")

    # Now add a link from current room to the new room
    new_exit = {"role": "link", "label": label_for_current_exit, "target": new_room["id"]}
    if len(current_room.get("exits", [])) == 0:
        current_room["exits"] = [new_exit]
    else:
        if len(current_room["exits"]) < 2:
            current_room["exits"].append(new_exit)
        else:
            current_room["exits"].append(new_exit)
    data["rooms"][str(current_id)] = current_room
    save_data(data)
    console.print(f"[green]Designed new room {new_room['id']} and linked it from room {current_id}.[/green]")
    return new_room

# ----------------------
# World Editor functions (unchanged)
# ----------------------
def editor_menu(data: Dict[str, Any]):
    while True:
        console.rule("[bold yellow]World Editor[/bold yellow]")
        print("Options:\n 1) Create room\n 2) List rooms\n 3) View room\n 4) Link exits (make an exit target an existing room)\n 5) Export map (print adjacency)\n 6) Back to main menu")
        choice = Prompt.ask("Choose", choices=["1", "2", "3", "4", "5", "6"], default="1")
        if choice == "1":
            create_manual_room(data)
        elif choice == "2":
            list_rooms(data)
        elif choice == "3":
            view_room_interactive(data)
        elif choice == "4":
            link_exits_interactive(data)
        elif choice == "5":
            export_map(data)
        elif choice == "6":
            break

def list_rooms(data: Dict[str, Any]):
    rooms = data.get("rooms", {})
    if not rooms:
        print("[dim]No rooms yet.[/dim]")
        return
    for rid in sorted(map(int, rooms.keys())):
        r = rooms[str(rid)]
        print(f"  id={r['id']} title={r['title']} coins={r.get('coins',0)}")

def view_room_interactive(data: Dict[str, Any]):
    rid = int(Prompt.ask("Room id to view", default="0"))
    rooms = data.get("rooms", {})
    if str(rid) not in rooms:
        print("[red]No such room id.[/red]")
        return
    r = rooms[str(rid)]
    console.rule(f"[bold]{r['title']} (id={r['id']})")
    print(r["description"])
    print(f"Coins: {r.get('coins',0)}")
    print("Exits:")
    for i, ex in enumerate(r["exits"], start=1):
        s = f"  {i}. label='{ex.get('label')}' role={ex.get('role')}"
        if ex.get("role") == "link":
            s += f" -> target={ex.get('target')}"
        print(s)

def link_exits_interactive(data: Dict[str, Any]):
    rid = int(Prompt.ask("Room id to edit exits for", default="0"))
    rooms = data.get("rooms", {})
    if str(rid) not in rooms:
        print("[red]No such room id.[/red]")
        return
    room = rooms[str(rid)]
    print("Current exits:")
    for i, ex in enumerate(room["exits"], start=1):
        print(f"  {i}. label={ex.get('label')} role={ex.get('role')} target={ex.get('target','')}")
    ex_i = int(Prompt.ask("Which exit number to link (1 or 2)?", choices=["1", "2"], default="1"))
    target = int(Prompt.ask("Target room id to link to", default="0"))
    room["exits"][ex_i - 1]["role"] = "link"
    room["exits"][ex_i - 1]["target"] = target
    save_data(data)
    print("[green]Exit linked.[/green]")

def export_map(data: Dict[str, Any]):
    rooms = data.get("rooms", {})
    print("[bold]Adjacency list:[/bold]")
    for rid in sorted(map(int, rooms.keys())):
        r = rooms[str(rid)]
        outs = []
        for ex in r["exits"]:
            role = ex.get("role")
            if role == "link":
                outs.append(f"-> {ex.get('target')}")
            else:
                outs.append(f"({role})")
        print(f" {rid}: {' , '.join(outs)}")

# ----------------------
# Play loop with design support
# ----------------------
def randomized_exits_for_display(room: Dict[str, Any]):
    exits = room.get("exits", [])
    order = list(range(len(exits)))
    random.shuffle(order)
    disp = {}
    for i, idx in enumerate(order, start=1):
        disp[str(i)] = exits[idx]
    return disp

def pick_existing_room(data: Dict[str, Any], exclude_id: int):
    ids = [int(k) for k in data["rooms"].keys() if int(k) != exclude_id]
    if not ids:
        return None
    picked = random.choice(ids)
    return data["rooms"][str(picked)]

def display_room(room: Dict[str, Any], player_coins: int):
    console.rule(f"[bold cyan]{room['title']} (id={room['id']})")
    print(room["description"])
    if room.get("image"):
        print(f"[dim]Image file: {room['image']}[/dim]")
    if room.get("coins", 0) > 0:
        print(f"[yellow]You see {room['coins']} coin(s) here.[/yellow]")
    print(f"[green]Your coins: {player_coins}[/green]")

def run_admin_create(data: Dict[str, Any], player_coins: int):
    title = Prompt.ask("Room title", default=f"Admin Room {data['next_id']}")
    desc = Prompt.ask("Description (short)", default="A room created by admin.")
    llabel = Prompt.ask("Exit 1 label", default="Left Door")
    rlabel = Prompt.ask("Exit 2 label", default="Right Door")
    coins_here = int(Prompt.ask("Coins in this room (0-5)", default="0"))
    exits = [
        {"role": "home_or_death", "label": llabel},
        {"role": "existing_or_new", "label": rlabel}
    ]
    admin_room = {"title": title, "description": desc, "exits": exits, "coins": coins_here}
    new_room = create_manual_room(data, manual=admin_room)
    return new_room, player_coins

def play_game_loop():
    data = load_data()
    current_id = data.get("start_room", 0)
    player_coins = 0

    if not ADMIN_PASS:
        import secrets
        autogen = secrets.token_urlsafe(12)
        print(f"[dim]No ADMIN_PASS set. If you want admin privileges later, set ADMIN_PASS to: {autogen}[/dim]")

    while True:
        room = data["rooms"][str(current_id)]
        if room.get("coins", 0) > 0:
            console.print(f"[yellow]You pick up {room['coins']} coin(s).[/yellow]")
            player_coins += room["coins"]
            room["coins"] = 0
            save_data(data)

        display_room(room, player_coins)

        disp_exits = randomized_exits_for_display(room)
        print("\nExits:")
        for key, ex in disp_exits.items():
            lbl = ex.get("label", "<exit>")
            if ex.get("role") == "link":
                lbl += f" -> {ex.get('target')}"
            print(f"  {key}. {lbl}")

        only_one_exit = len(room.get("exits", [])) == 1

        if only_one_exit:
            action = Prompt.ask("\nChoose exit (1) or command (design/editor/admin/quit/help)", default="1")
        else:
            action = Prompt.ask("\nChoose exit (1/2) or command (editor/admin/quit/help)", default="1")

        if action.lower() in ("q", "quit", "exit"):
            print("Goodbye.")
            break

        if action.lower() in ("help", "?"):
            print("Commands: 1 or 2 to choose exits. 'editor' launches the World Editor to add rooms. 'design' builds a room if the current room has only one exit. 'quit' exits.")
            continue

        if action.lower().startswith("editor"):
            editor_menu(data)
            continue

        if action.lower().startswith("design"):
            if not only_one_exit:
                print("[red]Design can only be used in rooms with exactly one built exit.[/red]")
                continue
            new_room = design_room_from_current(data, current_id)
            current_id = new_room["id"]
            continue

        if action.lower().startswith("admin"):
            if room["title"] != "Room Creation Computer":
                print("[red]Admin/create functions only available at the Room Creation Computer.[/red]")
                continue
            pw = Prompt.ask("Admin password (leave blank to use coins)", password=True, default="")
            if pw:
                if ADMIN_PASS and pw == ADMIN_PASS:
                    new_room, player_coins = run_admin_create(data, player_coins)
                    current_id = new_room["id"]
                    continue
                else:
                    print("[red]Invalid admin password.[/red]")
                    continue
            if player_coins >= COIN_COST:
                confirm = Prompt.ask(f"Spend {COIN_COST} coins to create a room? (y/n)", choices=["y", "n"], default="y")
                if confirm == "y":
                    player_coins -= COIN_COST
                    new_room, player_coins = run_admin_create(data, player_coins)
                    current_id = new_room["id"]
                    continue
                else:
                    print("Cancelled.")
                    continue
            else:
                print(f"[red]Not enough coins. You need {COIN_COST} coins to create a room.[/red]")
                continue

        if action in disp_exits:
            chosen = disp_exits[action]
            role = chosen.get("role")
            if role == "link" and "target" in chosen:
                tgt = chosen["target"]
                if str(tgt) in data["rooms"]:
                    current_id = tgt
                    continue
                else:
                    print("[red]Linked target room does not exist.[/red]")
            if role == "home_or_death":
                if random.random() < 0.75:
                    print("[green]You found your way home! Congratulations![/green]")
                    break
                else:
                    print("[red]A sudden chill. You die. Game over.[/red]")
                    break
            elif role == "existing_or_new":
                if random.random() < 0.25:
                    new_room = create_new_room_ai(data)
                    console.print(f"[green]A new room was generated: {new_room['title']} (id={new_room['id']})[/green]")
                    current_id = new_room["id"]
                else:
                    pick = pick_existing_room(data, exclude_id=room["id"])
                    if pick:
                        console.print(f"[cyan]You are transported to an existing room: {pick['title']} (id={pick['id']})[/cyan]")
                        current_id = pick["id"]
                    else:
                        console.print("[yellow]No existing rooms to transport to; creating one instead.[/yellow]")
                        new_room = create_new_room_ai(data)
                        current_id = new_room["id"]
            else:
                pick = pick_existing_room(data, exclude_id=room["id"])
                if pick:
                    current_id = pick["id"]
                else:
                    new_room = create_new_room_ai(data)
                    current_id = new_room["id"]
        else:
            print("[red]Unknown command. Choose a displayed exit number, or type help.[/red]")

# ----------------------
# Top-level menu
# ----------------------
def main_menu():
    while True:
        console.rule("[bold magenta]Portal Text Adventure[/bold magenta]")
        print("Menu:\n 1) Play game\n 2) World Editor (create rooms without AI)\n 3) Quit")
        choice = Prompt.ask("Choice", choices=["1", "2", "3"], default="1")
        if choice == "1":
            play_game_loop()
        elif choice == "2":
            data = load_data()
            editor_menu(data)
        elif choice == "3":
            print("Bye.")
            break

if __name__ == "__main__":
    main_menu()
# (file content ends here)