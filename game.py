"""
game.py - main CLI loop for the Portal Text Adventure.

Features:
- Loads/saves rooms.json (persistent map)
- Uses ai_adapter.generate_room_text / generate_room_image to create rooms
- Coins: rooms may contain coins; collected on entry
- Room Creation Computer appears every 10 generated rooms and requires COIN_COST to use
- ADMIN_PASS bypasses coin requirement (if set and provided)
- Exits are randomized on each visit for display, but room data persists
"""
import os
import json
import random
from pathlib import Path
from time import sleep
from typing import Dict, Any
from rich import print
from rich.prompt import Prompt
from rich.console import Console

import ai_adapter as ai

DATA_FILE = Path("rooms.json")
IMAGES_DIR = Path("images")
IMAGES_DIR.mkdir(exist_ok=True)
console = Console()

COIN_COST = int(os.environ.get("COIN_COST", "3"))
ADMIN_PASS = os.environ.get("ADMIN_PASS")
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "512x512")
NO_IMAGES = os.environ.get("NO_IMAGES", "") in ("1", "true", "True")

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
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data: Dict[str, Any]):
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def save_image(image_bytes: bytes, room_id: int) -> str:
    path = IMAGES_DIR / f"room_{room_id}.png"
    with open(path, "wb") as f:
        f.write(image_bytes)
    return str(path)

def pick_existing_room(data: Dict[str, Any], exclude_id: int):
    ids = [int(k) for k in data["rooms"].keys() if int(k) != exclude_id]
    if not ids:
        return None
    picked = random.choice(ids)
    return data["rooms"][str(picked)]

def create_new_room(data: Dict[str, Any], seed: str = None, admin_room: Dict[str, Any] = None) -> Dict[str, Any]:
    room_id = data["next_id"]
    data["next_id"] = room_id + 1

    if admin_room:
        room = {
            "id": room_id,
            "title": admin_room.get("title", f"Admin Room {room_id}"),
            "description": admin_room.get("description", ""),
            "image": "",
            "coins": admin_room.get("coins", 0),
            "exits": admin_room.get("exits", [
                {"role":"home_or_death","label":"Left"},
                {"role":"existing_or_new","label":"Right"}
            ])
        }
        data["rooms"][str(room_id)] = room
        save_data(data)
        return room

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
                {"role":"home_or_death","label":"Left Console Gate"},
                {"role":"existing_or_new","label":"Right Console Gate"}
            ]
        }
        data["rooms"][str(room_id)] = room
        save_data(data)
        return room

    title = room_json.get("title", f"Room {room_id}")
    description = room_json.get("description", "An indescribable place.")
    labels = room_json.get("exit_labels", {})
    exits = [
        {"role":"home_or_death","label":labels.get("1","Left")},
        {"role":"existing_or_new","label":labels.get("2","Right")}
    ]

    coins = 0
    if random.random() < 0.25:
        coins = random.choice([1,2])

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
            room["image"] = save_image(img_bytes, room_id)
    except Exception as e:
        console.print(f"[red]Image generation failed: {e}[/red]")
        room["image"] = ""

    data["rooms"][str(room_id)] = room
    save_data(data)
    return room

def display_room(room: Dict[str, Any], player_coins: int):
    console.rule(f"[bold cyan]{room['title']} (id={room['id']})")
    print(room["description"])
    if room.get("image"):
        print(f"[dim]Image file: {room['image']}[/dim]")
    if room.get("coins", 0) > 0:
        print(f"[yellow]You see {room['coins']} coin(s) here.[/yellow]")
    print(f"[green]Your coins: {player_coins}[/green]")

def randomized_exits_for_display(room: Dict[str, Any]) -> Dict[str, Dict]:
    exits = room["exits"]
    order = [0,1]
    random.shuffle(order)
    return {"1": exits[order[0]], "2": exits[order[1]]}

def run_admin_create(data: Dict[str, Any], player_coins: int):
    title = Prompt.ask("Room title", default=f"Admin Room {data['next_id']}")
    desc = Prompt.ask("Description (short)", default="A room created by admin.")
    llabel = Prompt.ask("Exit 1 label", default="Left Door")
    rlabel = Prompt.ask("Exit 2 label", default="Right Door")
    coins_here = int(Prompt.ask("Coins in this room (0-5)", default="0"))
    exits = [
        {"role":"home_or_death","label":llabel},
        {"role":"existing_or_new","label":rlabel}
    ]
    admin_room = {"title": title, "description": desc, "exits": exits, "coins": coins_here}
    new_room = create_new_room(data, admin_room=admin_room)
    console.print(f"[green]Admin created room {new_room['id']}[/green]")
    return new_room, player_coins

def play_game():
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
            print(f"  {key}. {ex['label']}")

        action = Prompt.ask("\nChoose exit (1/2) or command (admin/create/quit/help)", default="1")

        if action.lower() in ("q", "quit", "exit"):
            print("Goodbye.")
            break

        if action.lower() in ("help", "?"):
            print("Commands: 1 or 2 to choose exits. At Room Creation Computer use 'admin' to attempt to create a room (costs coins). 'quit' exits.")
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
                confirm = Prompt.ask(f"Spend {COIN_COST} coins to create a room? (y/n)", choices=["y","n"], default="y")
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

        if action == "1" or action == "2":
            chosen = disp_exits[action]
            role = chosen["role"]
            if role == "home_or_death":
                if random.random() < 0.75:
                    print("[green]You found your way home! Congratulations![/green]")
                    break
                else:
                    print("[red]A sudden chill. You die. Game over.[/red]")
                    break
            elif role == "existing_or_new":
                if random.random() < 0.25:
                    new_room = create_new_room(data)
                    console.print(f"[green]A new room was generated: {new_room['title']} (id={new_room['id']})[/green]")
                    current_id = new_room["id"]
                else:
                    pick = pick_existing_room(data, exclude_id=room["id"])
                    if pick:
                        console.print(f"[cyan]You are transported to an existing room: {pick['title']} (id={pick['id']})[/cyan]")
                        current_id = pick["id"]
                    else:
                        console.print("[yellow]No existing rooms to transport to; creating one instead.[/yellow]")
                        new_room = create_new_room(data)
                        current_id = new_room["id"]
            else:
                pick = pick_existing_room(data, exclude_id=room["id"])
                if pick:
                    current_id = pick["id"]
                else:
                    new_room = create_new_room(data)
                    current_id = new_room["id"]
        else:
            print("[red]Unknown command. Choose 1 or 2, or type quit/help.[/red]")

if __name__ == "__main__":
    play_game()