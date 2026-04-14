from flask import Flask, render_template_string, request, redirect, flash
from modules.db import init_db, add_member, get_members
from modules.notifier import generate_whatsapp_link

app = Flask(__name__)
app.secret_key = "12345"

init_db()

@app.route("/dashboard/members")
def members_page():
    members = get_members()
    return render_template_string("""
    <h2>Members</h2>

    <form method="POST" action="/add-member">
        Name: <input name="name"><br>
        Email: <input name="email"><br>
        WhatsApp (+91...): <input name="whatsapp"><br>
        <button>Add</button>
    </form>

    <hr>

    {% for m in members %}
        {{m.name}} - {{m.whatsapp}} <br>
    {% endfor %}

    <hr>

    <h3>Reminder</h3>
    <form method="POST" action="/send-reminder">
        Topic: <input name="topic"><br>
        Time: <input name="time"><br>
        <button>Send Reminder</button>
    </form>
    """, members=members)

@app.route("/add-member", methods=["POST"])
def add_member_route():
    add_member(
        request.form["name"],
        request.form["email"],
        request.form["whatsapp"]
    )
    return redirect("/dashboard/members")

@app.route("/send-reminder", methods=["POST"])
def send_reminder():
    topic = request.form.get("topic")
    time = request.form.get("time")

    members = get_members()

    message = f"📢 Zoom Reminder\\nTopic: {topic}\\nTime: {time}"

    links = []
    for m in members:
        if m["whatsapp"]:
            link = generate_whatsapp_link(m["whatsapp"], message)
            links.append(link)

    return render_template_string("""
    <h2>WhatsApp Reminder</h2>

    {% for link in links %}
        <a href="{{link}}" target="_blank">Open Chat</a><br><br>
    {% endfor %}

    <a href="/dashboard/members">Back</a>
    """, links=links)