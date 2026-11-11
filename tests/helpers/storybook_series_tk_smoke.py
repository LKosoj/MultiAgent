import os, tempfile, time
from pathlib import Path
rootdir = Path(tempfile.mkdtemp())
os.environ['STORYBOOK_PROJECTS_DIR'] = str(rootdir / 'projects')
from custom_tools.storybook.studio.capabilities import invoke
from StoryBookManager.config.settings import app_settings
app_settings.config_file = rootdir / 'settings.json'
app_settings.set('backup_directory', str(rootdir / 'backups'))
app_settings.set('logs_directory', str(rootdir / 'logs'))
app_settings.set('auto_save_interval', 0)
from tkinter import Tk
from StoryBookManager.gui.main_window import MainWindow
result = invoke('projects_create', {'title': 'World', 'description':'A persistent world', 'genre':'story', 'target_age':'6'})
assert result['status'] == 'success', result
pid = result['project_id']
old = invoke('shared_resource_save', {'project_id': pid, 'kind': 'characters', 'entity_id': 'hero', 'data': {'name': 'Hero', 'voice': 'v1'}})['version_id']
for name in ('First', 'Second'):
    result = invoke('series_create', {'project_id':pid, 'title':name, 'source_material':name, 'participant_ids':['hero'], 'settings':{}})
    assert result['status'] == 'success', result
root = Tk(); app = MainWindow(root)
# Verify the actual project list and details before opening the project.
end = time.monotonic()+5
while not app.project_panel.tree.exists(pid) and time.monotonic() < end:
    root.update(); time.sleep(.03)
assert app.project_panel.tree.exists(pid)
assert str(app.project_panel.tree.item(pid, 'values')[0]) == '2'
app.project_panel.tree.selection_set(pid)
end = time.monotonic()+5
while 'Герои: 1' not in app.project_panel.d_spend.get() and time.monotonic() < end:
    root.update(); time.sleep(.03)
assert 'Серий: 2' in app.project_panel.d_info.get()
assert 'Герои: 1' in app.project_panel.d_spend.get()
assert 'Страниц:' not in app.project_panel.d_info.get()
assert 'Этап:' not in app.project_panel.d_info.get()
project = app.project_manager.load_project(pid)
app.on_project_selected(project)
end = time.monotonic()+5
while len(app.series_panel.series) < 2 and time.monotonic() < end:
    root.update(); time.sleep(.03)
assert len(app.series_panel.series) == 2, app.series_panel.series
for series in app.series_panel.series:
    app.on_series_selected(project, series)
    root.update()
    assert app.current_project.project_path == project.project_path / 'series' / series['series_id']
    assert app.bridge.scoped_payload('project_status', {'project_id':pid})['series_id'] == series['series_id']
new = invoke('shared_resource_save', {'project_id': pid, 'kind': 'characters', 'entity_id': 'hero', 'data': {'name': 'Hero', 'voice': 'v2'}})['version_id']
from tkinter import ttk, simpledialog, messagebox
simpledialog.askstring = lambda *args, **kwargs: new
messagebox.askyesno = lambda *args, **kwargs: True
first = app.series_panel.series[0]['series_id']
second = app.series_panel.series[1]['series_id']
app.series_panel.tree.selection_set(first)
app.series_panel.versions()
window = app.series_panel.winfo_children()[-1]
tree = next(child for child in window.winfo_children() if isinstance(child, ttk.Treeview))
end = time.monotonic()+5
while not tree.get_children() and time.monotonic() < end:
    root.update(); time.sleep(.03)
assert tree.get_children()
tree.selection_set(tree.get_children()[0])
next(child for child in window.winfo_children() if isinstance(child, ttk.Button)).invoke()
end = time.monotonic()+5
while time.monotonic() < end:
    root.update(); time.sleep(.03)
    a = invoke('series_get', {'project_id': pid, 'series_id': first})['series']['snapshot']['characters']['hero']
    if a == new: break
assert a == new
b = invoke('series_get', {'project_id': pid, 'series_id': second})['series']['snapshot']['characters']['hero']
assert b == old
# Open forms in A, switch to B with the same series ID, then try every save.
result = invoke('projects_create', {'title': 'Other World', 'description': 'Another persistent world', 'genre': 'story', 'target_age': '6'})
pid_b = result['project_id']
invoke('shared_resource_save', {'project_id': pid_b, 'kind': 'characters', 'entity_id': 'hero', 'data': {'name': 'Other hero', 'voice': 'other'}})
first_item = next(item for item in app.series_panel.series if item['series_id'] == first)
created_b = invoke('series_create', {'project_id': pid_b, 'title': first_item['title'], 'source_material': 'Other material', 'participant_ids': ['hero'], 'settings': {}})
assert created_b['series_id'] == first
project_b = app.project_manager.load_project(pid_b)
screen = app.series_panel
warnings = []
messagebox.showwarning = lambda *args, **kwargs: warnings.append(args)
def switch(target):
    app.on_project_selected(target)
    end = time.monotonic() + 5
    while screen.data.get('project_id') != target.project_id and time.monotonic() < end:
        root.update(); time.sleep(.03)
    assert screen.data.get('project_id') == target.project_id
    while not screen.tree.exists(first) and time.monotonic() < end:
        root.update(); time.sleep(.03)
    assert screen.tree.exists(first)
    screen.tree.selection_set(first); root.update()
def files_snapshot():
    result = {}
    for obj in (project, project_b):
        for file in obj.project_path.rglob('*'):
            if file.is_file() and (file.name == 'project.json' or file.name == 'series.json' or file.name == 'data.json'):
                result[str(file)] = file.read_bytes()
    return result
before = files_snapshot()
switch(project)
screen.resources()
resource = screen.winfo_children()[-1]
form = resource.winfo_children()[0].inner
children = form.winfo_children()
labels = {child.cget('text'): children[index + 1] for index, child in enumerate(children[:-1]) if isinstance(child, ttk.Label)}
labels['Идентификатор'].insert(0, 'wrong')
labels['Имя'].insert(0, 'Must stay in A')
resource_save = next(child for child in children if isinstance(child, ttk.Button) and child.cget('text') == 'Сохранить новую версию')
end = time.monotonic() + 5
while 'disabled' in resource_save.state() and time.monotonic() < end:
    root.update(); time.sleep(.03)
assert 'disabled' not in resource_save.state()
switch(project_b)
next(child for child in children if isinstance(child, ttk.Button) and child.cget('text') == 'Сохранить новую версию').invoke()
assert labels['Имя'].get() == 'Must stay in A' and resource.winfo_exists()
resource.destroy()
switch(project)
screen.create()
create_window = screen.winfo_children()[-1]
create_title = next(child for child in create_window.winfo_children() if isinstance(child, ttk.Entry))
create_title.insert(0, 'Unsaved A')
switch(project_b)
next(child for child in create_window.winfo_children() if isinstance(child, ttk.Button) and child.cget('text') == 'Сохранить').invoke()
assert create_title.get() == 'Unsaved A' and create_window.winfo_exists()
create_window.destroy()
switch(project)
screen.edit()
edit_window = screen.winfo_children()[-1]
edit_title = next(child for child in edit_window.winfo_children() if isinstance(child, ttk.Entry))
edit_title.delete(0, 'end'); edit_title.insert(0, 'Changed A')
switch(project_b)
next(child for child in edit_window.winfo_children() if isinstance(child, ttk.Button) and child.cget('text') == 'Сохранить').invoke()
assert edit_title.get() == 'Changed A' and edit_window.winfo_exists()
edit_window.destroy()
switch(project)
screen.versions()
version_window = screen.winfo_children()[-1]
version_tree = next(child for child in version_window.winfo_children() if isinstance(child, ttk.Treeview))
end = time.monotonic() + 5
while not version_tree.get_children() and time.monotonic() < end:
    root.update(); time.sleep(.03)
assert version_tree.get_children()
version_tree.selection_set(version_tree.get_children()[0])
switch(project_b)
next(child for child in version_window.winfo_children() if isinstance(child, ttk.Button)).invoke()
assert version_window.winfo_exists()
version_window.destroy()
switch(project)
screen.edit_project()
project_window = screen.winfo_children()[-1]
project_entries = [child for child in project_window.winfo_children() if isinstance(child, ttk.Entry)]
project_entries[0].delete(0, 'end'); project_entries[0].insert(0, 'Changed world')
project_entries[2].delete(0, 'end'); project_entries[2].insert(0, 'en')
switch(project_b)
next(child for child in project_window.winfo_children() if isinstance(child, ttk.Button) and child.cget('text') == 'Сохранить').invoke()
assert project_entries[0].get() == 'Changed world' and project_entries[2].get() == 'en' and project_window.winfo_exists()
project_window.destroy()
assert len(warnings) >= 5
assert files_snapshot() == before
# The same project form also saves an explicitly changed language in its own scope.
switch(project_b)
screen.edit_project()
project_window = screen.winfo_children()[-1]
project_entries = [child for child in project_window.winfo_children() if isinstance(child, ttk.Entry)]
project_entries[2].delete(0, 'end'); project_entries[2].insert(0, 'en')
next(child for child in project_window.winfo_children() if isinstance(child, ttk.Button) and child.cget('text') == 'Сохранить').invoke()
end = time.monotonic() + 5
while time.monotonic() < end:
    root.update(); time.sleep(.03)
    if invoke('projects_get', {'project_id': pid_b})['details']['language'] == 'en': break
assert invoke('projects_get', {'project_id': pid_b})['details']['language'] == 'en'
# Refreshing the list keeps the selected series and its open sound window usable.
switch(project_b)
b_audio = project_b.project_path / 'series' / first / '98_audio'
b_audio.mkdir(parents=True, exist_ok=True)
(b_audio / 'effect.wav').write_bytes(b'effect')
(b_audio / 'audio_manifest.json').write_text(__import__('json').dumps({'audio_tracks': [{'id': 'effect', 'role': 'sfx', 'path': 'effect.wav', 'offset_seconds': 0, 'duration_seconds': 1, 'level': 1, 'enabled': True}]}))
def descendants_for_test(widget):
    return [child for item in widget.winfo_children() for child in [item, *descendants_for_test(item)]]
screen.sound()
sound_dialog = screen.winfo_children()[-1]
end = time.monotonic() + 5
while time.monotonic() < end:
    root.update(); time.sleep(.03)
    lists = [w for w in descendants_for_test(sound_dialog) if w.winfo_class() == 'Listbox']
    if len(lists) > 1 and lists[1].size(): break
assert len(lists) > 1 and lists[1].size() == 1
lists[1].selection_set(0)
generation = screen._selection_generation
screen._show({'series': list(screen.series)})
root.update()
assert screen.tree.selection() == (first,) and screen._selection_generation == generation
simpledialog.askfloat = lambda *args, **kwargs: .2 if 'Громкость' in args[1] else 0
messagebox.askyesno = lambda *args, **kwargs: True
next(w for w in descendants_for_test(sound_dialog) if isinstance(w, ttk.Button) and w.cget('text') == 'Изменить').invoke()
end = time.monotonic() + 5
while time.monotonic() < end:
    root.update(); time.sleep(.03)
    track = __import__('json').loads((b_audio / 'audio_manifest.json').read_text())['audio_tracks'][0]
    if track.get('level') == .2: break
assert track['level'] == .2
sound_dialog.destroy()
(b_audio / 'audio_manifest.json').unlink()
# Editing before the project resources load must not empty existing participants.
switch(project_b)
before_series = invoke('series_get', {'project_id': pid_b, 'series_id': first})['series']
before_participants = before_series['participant_ids']
screen.data = {}
screen.edit()
edit_window = screen.winfo_children()[-1]
next(child for child in edit_window.winfo_children() if isinstance(child, ttk.Button) and child.cget('text') == 'Сохранить').invoke()
end = time.monotonic() + 5
while edit_window.winfo_exists() and time.monotonic() < end:
    root.update(); time.sleep(.03)
assert not edit_window.winfo_exists()
after_series = invoke('series_get', {'project_id': pid_b, 'series_id': first})['series']
assert after_series['participant_ids'] == before_participants
assert after_series['snapshot'] == before_series['snapshot']
# Empty screenplay and sound lists show neutral text.
switch(project_b)
screen.sound()
sound_window = screen.winfo_children()[-1]
end = time.monotonic() + 5
def descendants(widget):
    return [child for item in widget.winfo_children() for child in [item, *descendants(item)]]
while time.monotonic() < end:
    root.update(); time.sleep(.03)
    texts = [widget.cget('text') for widget in descendants(sound_window) if isinstance(widget, ttk.Label)]
    if 'Сценарий ещё не подготовлен' in texts and 'Звуки ещё не созданы' in texts and all(widget.winfo_manager() for widget in descendants(sound_window) if isinstance(widget, ttk.Label) and widget.cget('text') in ('Сценарий ещё не подготовлен', 'Звуки ещё не созданы')):
        break
assert 'Сценарий ещё не подготовлен' in texts and 'Звуки ещё не созданы' in texts
assert all(widget.winfo_manager() for widget in descendants(sound_window) if isinstance(widget, ttk.Label) and widget.cget('text') in ('Сценарий ещё не подготовлен', 'Звуки ещё не созданы'))
sound_window.destroy()
# A project or series selected again must not accept an earlier response.
from types import SimpleNamespace
screen = app.series_panel
original_bridge = screen.bridge
class DelayedBridge:
    def __init__(self): self.calls = []
    def call(self, name, payload, **callbacks): self.calls.append((name, callbacks))
delayed = DelayedBridge(); screen.bridge = delayed
screen.set_project(project)
old_project_callback = delayed.calls[0][1]['on_success']
screen.set_project(SimpleNamespace(project_id='other', name='Other'))
screen.set_project(project)
old_project_callback({'details': {'title': 'old response'}})
assert screen.data == {}
screen._show({'series': [{'series_id': 'one', 'title': 'One'}, {'series_id': 'two', 'title': 'Two'}]})
screen.tree.selection_set('one'); root.update()
seen = []
screen.call('speech_get', {'series_id': 'one'}, seen.append)
old_series_callback = delayed.calls[-1][1]['on_success']
screen.tree.selection_set('two'); root.update()
screen.tree.selection_set('one'); root.update()
old_series_callback({'old': True})
assert seen == []
screen.bridge = original_bridge
# The resource form must forget fields and pending reads when its identity changes.
screen = app.series_panel
screen.data = {'resources': {'characters': {'hero': old}}}
requests = []
def resource_test_call(name, payload, callback):
    requests.append((name, payload, callback))
    if name == 'projects_get': callback(invoke(name, {'project_id': pid, **payload}))
screen.call = resource_test_call
screen.resources()
resource_window = screen.winfo_children()[-1]
form = resource_window.winfo_children()[0].inner
children = form.winfo_children()
def field(label):
    index = next(i for i, child in enumerate(children) if isinstance(child, ttk.Label) and child.cget('text') == label)
    return children[index + 1]
def button(label):
    return next(child for child in children if isinstance(child, ttk.Button) and child.cget('text') == label)
identifier = field('Идентификатор')
identifier.insert(0, 'hero')
button('Загрузить текущую версию').invoke()
pending = requests[-1][2]
pending({'data': {'name': 'Old Hero', 'private_field': 'old-data', 'reference_image_path': 'reference.png'}})
assert field('Имя').get() == 'Old Hero'
identifier.delete(0, 'end'); identifier.insert(0, 'newhero')
assert field('Имя').get() == ''
# A delayed response to the former identity cannot populate the new form.
pending({'data': {'name': 'Late Hero', 'private_field': 'late-data'}})
assert field('Имя').get() == ''
field('Имя').insert(0, 'New Hero')
button('Сохранить новую версию').invoke()
name, payload, _ = requests[-1]
assert name == 'shared_resource_save' and payload['entity_id'] == 'newhero'
assert payload['data']['name'] == 'New Hero'
assert 'private_field' not in payload['data'] and 'reference_image_path' not in payload['data']
resource_window.destroy()
# Existing resources of every kind cannot be saved before the current version loads.
for kind in ('characters', 'narrators', 'locations', 'items', 'style'):
    created = invoke('shared_resource_save', {'project_id': pid, 'kind': kind, 'entity_id': 'guard', 'data': {'name': 'Original', 'marker': kind}})
    assert created['status'] == 'success'
    version = created['version_id']
    screen.data = invoke('projects_get', {'project_id': pid})['details']
    requests = []
    def delayed_resource_call(name, payload, callback):
        requests.append((name, payload, callback))
        if name == 'projects_get': callback(invoke(name, {'project_id': pid, **payload}))
        if name == 'shared_resource_save': callback(invoke(name, {'project_id': pid, **payload}))
    screen.call = delayed_resource_call
    screen.resources()
    resource_window = screen.winfo_children()[-1]
    form = resource_window.winfo_children()[0].inner
    children = form.winfo_children()
    def field(label):
        index = next(i for i, child in enumerate(children) if isinstance(child, ttk.Label) and child.cget('text') == label)
        return children[index + 1]
    def button(label):
        return next(child for child in children if isinstance(child, ttk.Button) and child.cget('text') == label)
    field('Тип').set(kind)
    identifier = field('Идентификатор')
    identifier.insert(0, 'brandnew')
    save = button('Сохранить новую версию')
    assert 'disabled' not in save.state(), (kind, save.state())
    identifier.delete(0, 'end'); identifier.insert(0, 'guard')
    assert 'disabled' in save.state(), (kind, save.state())
    save.invoke()
    assert not any(name == 'shared_resource_save' for name, *_ in requests)
    button('Загрузить текущую версию').invoke()
    assert 'disabled' in save.state()
    save.invoke()
    assert not any(name == 'shared_resource_save' for name, *_ in requests)
    assert invoke('projects_get', {'project_id': pid})['details']['resources'][kind]['guard'] == version
    first_pending = requests[-1][2]
    # An unanswered or failed read leaves the action disabled; retry remains possible.
    button('Загрузить текущую версию').invoke()
    pending = requests[-1][2]
    result = invoke('shared_resource_get', {'project_id': pid, 'kind': kind, 'entity_id': 'guard', 'version_id': version})
    first_pending(result)
    assert 'disabled' in save.state()
    pending(result)
    assert 'disabled' not in save.state()
    field('Имя').delete(0, 'end'); field('Имя').insert(0, 'Changed')
    save.invoke()
    assert any(name == 'shared_resource_save' for name, *_ in requests)
    current = invoke('projects_get', {'project_id': pid})['details']['resources'][kind]['guard']
    assert current != version
    data = invoke('shared_resource_get', {'project_id': pid, 'kind': kind, 'entity_id': 'guard', 'version_id': current})['data']
    assert data['name'] == 'Changed' and data['marker'] == kind
app._shutdown_shell(); root.destroy()
print('Two-series Tk check passed')
