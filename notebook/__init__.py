"""The notebook module: the reference's note store, editor rules, Day view, Topic hub, renderer
and exporter, ported into Helm .

    notebook.config        the profile (work | home) and every path/mode from the env
    notebook.notes         the note store and index: text, revisions, links, tasks, FTS5
    notebook.edit          every write to a note, and the rule on each
    notebook.export        the one-way export: store -> folder (store mode)
    notebook.cutover       the flip from folder-as-truth to store-as-truth
    notebook.render        markdown -> HTML with wikilinks, embeds, checkboxes
    notebook.day           the Day view: note + thread timeline + pages changed that day
    notebook.topic         the Topic hub: threads about a note
    notebook.helm_adapter  the reference's helm_client, in-process over helm.store / helm.actions

The HTTP surface is app/notebook_api.py. See notebook/README.md.
"""
