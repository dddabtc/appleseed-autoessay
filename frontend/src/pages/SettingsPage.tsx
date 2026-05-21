import { FormEvent, useCallback, useEffect, useState } from "react";

import {
  Author,
  AuthorPayload,
  createAuthor,
  deleteAuthor,
  listAuthors,
  patchAuthor,
} from "../lib/api";
import { useT } from "../lib/i18n";

interface FormState {
  display_name: string;
  affiliation: string;
  email: string;
  orcid: string;
}

const EMPTY_FORM: FormState = {
  display_name: "",
  affiliation: "",
  email: "",
  orcid: "",
};

function formToPayload(form: FormState): AuthorPayload {
  return {
    display_name: form.display_name,
    affiliation: form.affiliation || null,
    email: form.email || null,
    orcid: form.orcid || null,
  };
}

function authorToForm(author: Author): FormState {
  return {
    display_name: author.display_name,
    affiliation: author.affiliation ?? "",
    email: author.email ?? "",
    orcid: author.orcid ?? "",
  };
}

export default function SettingsPage() {
  const t = useT();
  const [authors, setAuthors] = useState<Author[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | "new" | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [isSaving, setIsSaving] = useState(false);

  const reload = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const rows = await listAuthors();
      setAuthors(rows);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Load failed");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  function startNew() {
    setEditingId("new");
    setForm(EMPTY_FORM);
    setError(null);
  }

  function startEdit(author: Author) {
    setEditingId(author.id);
    setForm(authorToForm(author));
    setError(null);
  }

  function cancelEdit() {
    setEditingId(null);
    setForm(EMPTY_FORM);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingId) return;
    setIsSaving(true);
    setError(null);
    try {
      if (editingId === "new") {
        await createAuthor(formToPayload(form));
      } else {
        await patchAuthor(editingId, formToPayload(form));
      }
      cancelEdit();
      await reload();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Save failed");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleDelete(author: Author) {
    if (author.is_self) {
      setError(t("authors.cannot_delete_self"));
      return;
    }
    if (!window.confirm(t("authors.delete_confirm"))) return;
    setError(null);
    try {
      await deleteAuthor(author.id);
      await reload();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Delete failed");
    }
  }

  return (
    <section className="grid gap-6">
      <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm sm:p-6">
        <h1 className="text-2xl font-bold text-slate-950 sm:text-3xl">
          {t("settings.heading")}
        </h1>
        <h2 className="mt-4 text-lg font-bold text-slate-950">
          {t("settings.authors_section")}
        </h2>
        <p className="mt-1 leading-7 text-slate-700">
          {t("settings.authors_hint")}
        </p>
        <button
          type="button"
          data-testid="settings-add-author-button"
          onClick={startNew}
          className="mt-4 inline-flex min-h-10 items-center rounded-md bg-[#114b5f] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#0d3d4d]"
        >
          {t("authors.add_button")}
        </button>
      </div>

      {error ? (
        <p className="rounded-md bg-red-50 px-4 py-3 text-red-700">{error}</p>
      ) : null}

      {editingId ? (
        <form
          onSubmit={handleSubmit}
          className="grid gap-3 rounded-lg border border-slate-300 bg-white p-5 shadow-sm sm:p-6"
        >
          <label className="grid gap-1 text-sm font-semibold text-slate-800">
            {t("authors.display_name")}
            <input
              required
              value={form.display_name}
              onChange={(event) =>
                setForm((f) => ({ ...f, display_name: event.target.value }))
              }
              className="min-h-10 rounded-md border border-slate-300 px-3 py-1.5 font-normal text-slate-950 outline-none focus:border-[#114b5f]"
            />
          </label>
          <label className="grid gap-1 text-sm font-semibold text-slate-800">
            {t("authors.affiliation")}
            <input
              value={form.affiliation}
              onChange={(event) =>
                setForm((f) => ({ ...f, affiliation: event.target.value }))
              }
              className="min-h-10 rounded-md border border-slate-300 px-3 py-1.5 font-normal text-slate-950 outline-none focus:border-[#114b5f]"
            />
          </label>
          <label className="grid gap-1 text-sm font-semibold text-slate-800">
            {t("authors.email")}
            <input
              type="email"
              value={form.email}
              onChange={(event) =>
                setForm((f) => ({ ...f, email: event.target.value }))
              }
              className="min-h-10 rounded-md border border-slate-300 px-3 py-1.5 font-normal text-slate-950 outline-none focus:border-[#114b5f]"
            />
          </label>
          <label className="grid gap-1 text-sm font-semibold text-slate-800">
            {t("authors.orcid")}
            <input
              value={form.orcid}
              onChange={(event) =>
                setForm((f) => ({ ...f, orcid: event.target.value }))
              }
              placeholder="0000-0002-1825-0097"
              className="min-h-10 rounded-md border border-slate-300 px-3 py-1.5 font-normal text-slate-950 outline-none focus:border-[#114b5f]"
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              data-testid="settings-author-save-button"
              disabled={isSaving}
              className="inline-flex min-h-10 items-center rounded-md bg-[#114b5f] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#0d3d4d] disabled:opacity-60"
            >
              {t("authors.save_button")}
            </button>
            <button
              type="button"
              data-testid="settings-author-cancel-button"
              onClick={cancelEdit}
              className="inline-flex min-h-10 items-center rounded-md bg-slate-100 px-4 py-2 text-sm font-bold text-[#114b5f] transition hover:bg-slate-200"
            >
              {t("authors.cancel_button")}
            </button>
          </div>
        </form>
      ) : null}

      {isLoading ? (
        <p className="rounded-lg border border-slate-200 bg-white px-4 py-3 text-slate-700">
          Loading…
        </p>
      ) : authors.length === 0 ? (
        <p className="rounded-lg border border-slate-200 bg-white px-4 py-3 text-slate-700">
          {t("authors.empty_hint")}
        </p>
      ) : (
        <ul className="grid list-none gap-2 p-0">
          {authors.map((author) => (
            <li key={author.id}>
              <div className="grid gap-2 rounded-lg border border-slate-200 bg-white p-4 shadow-sm sm:flex sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="text-slate-950">
                      {author.display_name}
                    </strong>
                    {author.is_self ? (
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-bold text-[#114b5f]">
                        {t("authors.self_label")}
                      </span>
                    ) : null}
                    {author.deleted_at ? (
                      <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs font-bold text-slate-700">
                        {t("authors.deleted_badge")}
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-1 text-sm text-slate-600">
                    {author.affiliation ? (
                      <div>{author.affiliation}</div>
                    ) : null}
                    {author.email ? <div>{author.email}</div> : null}
                    {author.orcid ? (
                      <div className="font-mono text-xs">{author.orcid}</div>
                    ) : null}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    data-testid={`settings-author-edit-${author.id}`}
                    onClick={() => startEdit(author)}
                    className="inline-flex min-h-9 items-center rounded-md bg-slate-100 px-3 py-1 text-xs font-bold text-[#114b5f] transition hover:bg-slate-200"
                  >
                    {t("authors.save_button") /* edit shares Save copy */}
                  </button>
                  {!author.is_self && !author.deleted_at ? (
                    <button
                      type="button"
                      data-testid={`settings-author-delete-${author.id}`}
                      onClick={() => void handleDelete(author)}
                      className="inline-flex min-h-9 items-center rounded-md bg-slate-100 px-3 py-1 text-xs font-bold text-red-700 transition hover:bg-red-50"
                    >
                      {t("authors.delete_button")}
                    </button>
                  ) : null}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
