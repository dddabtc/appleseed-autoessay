import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ChangeEvent,
  type DragEvent
} from 'react';
import { Link } from 'react-router';

import {
  deleteCorpusDocument,
  getCorpusStyleProfile,
  listCorpusDocuments,
  rebuildCorpusStyleProfile,
  uploadCorpusDocument,
  type CorpusDocument,
  type StyleProfileSummary
} from '../lib/api';
import { useT } from '../lib/i18n';

const primaryButtonClasses =
  'inline-flex min-h-11 w-full items-center justify-center rounded-md bg-[#114b5f] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#0d3d4d] disabled:cursor-default disabled:opacity-65 sm:w-auto';

const secondaryButtonClasses =
  'inline-flex min-h-11 w-full items-center justify-center rounded-md bg-slate-100 px-4 py-2 text-sm font-bold text-[#114b5f] transition hover:bg-slate-200 disabled:cursor-default disabled:opacity-65 sm:w-auto';

export default function CorpusPage() {
  const t = useT();
  const [documents, setDocuments] = useState<CorpusDocument[]>([]);
  const [profile, setProfile] = useState<StyleProfileSummary | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [isUploadOpen, setIsUploadOpen] = useState(false);
  const [isRebuilding, setIsRebuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const nextDocuments = await listCorpusDocuments();
      setDocuments(nextDocuments);
      try {
        setProfile(await getCorpusStyleProfile());
      } catch {
        setProfile(null);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Corpus fetch failed');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleUpload(file: File) {
    setIsUploading(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.set('file', file);
      await uploadCorpusDocument(formData);
      setIsUploadOpen(false);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Upload failed');
    } finally {
      setIsUploading(false);
    }
  }

  async function handleDelete(documentId: string) {
    setError(null);
    try {
      await deleteCorpusDocument(documentId);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Delete failed');
    }
  }

  async function handleRebuild() {
    setIsRebuilding(true);
    setError(null);
    try {
      await rebuildCorpusStyleProfile();
      await pollProfile();
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Style profile rebuild failed');
    } finally {
      setIsRebuilding(false);
    }
  }

  async function pollProfile() {
    for (let attempt = 0; attempt < 6; attempt += 1) {
      try {
        setProfile(await getCorpusStyleProfile());
        return;
      } catch {
        await new Promise((resolve) => window.setTimeout(resolve, 700));
      }
    }
  }

  return (
    <section className="grid gap-6">
      <div className="grid gap-4 lg:flex lg:items-start lg:justify-between">
        <div>
          <p className="mb-2 text-xs font-bold uppercase text-slate-500">
            {t('corpus.section_label')}
          </p>
          <h1 className="text-2xl font-bold text-slate-950 sm:text-3xl">
            {t('corpus.heading')}
          </h1>
        </div>
        <div className="grid gap-2 sm:flex sm:flex-wrap sm:justify-end">
          <button
            type="button"
            data-testid="corpus-rebuild-profile-button"
            className={secondaryButtonClasses}
            onClick={handleRebuild}
            disabled={isRebuilding || documents.length === 0}
          >
            {isRebuilding ? 'Rebuilding...' : t('corpus.rebuild_profile')}
          </button>
          <button
            type="button"
            data-testid="corpus-upload-prior-button"
            className={secondaryButtonClasses}
            onClick={() => setIsUploadOpen(true)}
          >
            {t('corpus.upload_prior')}
          </button>
        </div>
      </div>
      <div className="grid gap-3 rounded-lg border-2 border-[#114b5f] bg-[#114b5f]/5 p-4 sm:p-5">
        <p className="text-sm leading-6 text-slate-700">
          {t('corpus.cta_help')}
        </p>
        <Link
          to="/runs/new"
          data-testid="corpus-start-project-link"
          className="inline-flex min-h-12 w-full items-center justify-center rounded-md bg-[#114b5f] px-4 py-2 text-base font-bold text-white no-underline transition hover:bg-[#0d3d4d] sm:w-auto sm:self-start"
        >
          {t('corpus.cta_start_project')}
        </Link>
      </div>
      <div className="rounded-lg border border-[#236b45]/25 bg-[#236b45]/10 p-4 leading-7 text-[#174c32]">
        {t('corpus.privacy_notice')}
      </div>
      {error ? <p className="leading-7 text-red-700">{error}</p> : null}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(20rem,0.8fr)] lg:items-start">
        <CorpusDocumentsPanel
          documents={documents}
          isLoading={isLoading}
          onDelete={handleDelete}
        />
        <StyleProfilePanel profile={profile} />
      </div>
      {isUploadOpen ? (
        <UploadModal
          isUploading={isUploading}
          onUpload={handleUpload}
          onClose={() => setIsUploadOpen(false)}
        />
      ) : null}
    </section>
  );
}

function CorpusDocumentsPanel({
  documents,
  isLoading,
  onDelete
}: {
  documents: CorpusDocument[];
  isLoading: boolean;
  onDelete: (documentId: string) => Promise<void>;
}) {
  const t = useT();
  if (isLoading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <p className="leading-7 text-slate-700">Loading corpus...</p>
      </div>
    );
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
      <h2 className="text-lg font-bold text-slate-950">
        {t('corpus.uploaded_papers')}
      </h2>
      {documents.length === 0 ? (
        <p className="mt-3 leading-7 text-slate-700">
          No prior papers uploaded yet.
        </p>
      ) : (
        <>
          <div className="mt-4 hidden overflow-x-auto md:block">
            <table className="w-full border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-slate-500">
                  <th className="py-3 pr-3 font-bold">Title</th>
                  <th className="px-3 py-3 font-bold">Type</th>
                  <th className="px-3 py-3 font-bold">Status</th>
                  <th className="px-3 py-3 font-bold">Size</th>
                  <th className="px-3 py-3 font-bold">Uploaded</th>
                  <th className="py-3 pl-3 text-right font-bold">Actions</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((document) => (
                  <tr className="border-b border-slate-100" key={document.id}>
                    <td className="py-3 pr-3 font-semibold text-slate-950">
                      {document.title}
                    </td>
                    <td className="px-3 py-3 text-slate-700">
                      {document.document_type}
                    </td>
                    <td className="px-3 py-3">
                      <StatusBadge status={document.ingest_status} />
                    </td>
                    <td className="px-3 py-3 text-slate-700">
                      {formatBytes(document.original_size_bytes)}
                    </td>
                    <td className="px-3 py-3 text-slate-700">
                      {formatDate(document.created_at)}
                    </td>
                    <td className="py-3 pl-3 text-right">
                      <button
                        type="button"
                        data-testid={`corpus-doc-delete-${document.id}`}
                        className="min-h-10 rounded-md bg-slate-100 px-3 py-2 text-sm font-bold text-red-700 transition hover:bg-red-50"
                        onClick={() => void onDelete(document.id)}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="mt-4 grid gap-3 p-0 md:hidden">
            {documents.map((document) => (
              <li
                className="grid list-none gap-3 rounded-lg border border-slate-200 p-4"
                key={document.id}
              >
                <div className="flex items-start justify-between gap-3">
                  <strong className="min-w-0 text-slate-950">
                    {document.title}
                  </strong>
                  <StatusBadge status={document.ingest_status} />
                </div>
                <dl className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <dt className="font-bold text-slate-500">{t('corpus.type')}</dt>
                    <dd className="m-0 text-slate-700">
                      {document.document_type}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-bold text-slate-500">{t('corpus.size')}</dt>
                    <dd className="m-0 text-slate-700">
                      {formatBytes(document.original_size_bytes)}
                    </dd>
                  </div>
                  <div className="col-span-2">
                    <dt className="font-bold text-slate-500">{t('corpus.uploaded')}</dt>
                    <dd className="m-0 text-slate-700">
                      {formatDate(document.created_at)}
                    </dd>
                  </div>
                </dl>
                <button
                  type="button"
                  data-testid={`corpus-doc-delete-mobile-${document.id}`}
                  className={secondaryButtonClasses}
                  onClick={() => void onDelete(document.id)}
                >
                  {t('corpus.delete')}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function UploadModal({
  isUploading,
  onUpload,
  onClose
}: {
  isUploading: boolean;
  onUpload: (file: File) => Promise<void>;
  onClose: () => void;
}) {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const selectedLabel = useMemo(
    () => selectedFile?.name ?? 'PDF, DOCX, MD, or TXT up to 30 MB',
    [selectedFile]
  );

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    setSelectedFile(event.target.files?.[0] ?? null);
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setIsDragging(false);
    setSelectedFile(event.dataTransfer.files[0] ?? null);
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4">
      <section className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <p className="mb-2 text-xs font-bold uppercase text-slate-500">
              Upload
            </p>
            <h2 className="text-xl font-bold text-slate-950">
              Upload prior paper
            </h2>
          </div>
          <button
            type="button"
            data-testid="corpus-upload-modal-close"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md bg-slate-100 text-xl font-bold text-[#114b5f] transition hover:bg-slate-200"
            aria-label="Close upload dialog"
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <label
          className={`grid min-h-40 cursor-pointer place-items-center rounded-lg border-2 border-dashed p-5 text-center transition ${
            isDragging
              ? 'border-[#114b5f] bg-[#114b5f]/5'
              : 'border-slate-300 bg-slate-50'
          }`}
          onDragOver={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={handleDrop}
        >
          <span className="font-semibold text-slate-800">{selectedLabel}</span>
          <input
            data-testid="corpus-upload-file-input"
            className="sr-only"
            type="file"
            accept=".pdf,.docx,.md,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/markdown,text/plain"
            onChange={handleFileChange}
          />
        </label>
        <div className="mt-5 grid gap-2 sm:flex sm:justify-end">
          <button
            type="button"
            data-testid="corpus-upload-modal-cancel"
            className={secondaryButtonClasses}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="corpus-upload-modal-submit"
            className={primaryButtonClasses}
            disabled={!selectedFile || isUploading}
            onClick={() => {
              if (selectedFile) {
                void onUpload(selectedFile);
              }
            }}
          >
            {isUploading ? 'Uploading...' : 'Upload'}
          </button>
        </div>
      </section>
    </div>
  );
}

function StyleProfilePanel({ profile }: { profile: StyleProfileSummary | null }) {
  const t = useT();
  return (
    <aside className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
      <h2 className="text-lg font-bold text-slate-950">
        {t('corpus.profile_heading')}
      </h2>
      {profile ? (
        <div className="mt-4 grid gap-4 text-sm">
          <ProfileDiagnostics profile={profile} />
          <DistributionRow
            label={t('corpus.profile.paragraph_length')}
            distribution={profile.paragraph_length_distribution}
          />
          <DistributionRow
            label={t('corpus.profile.sentence_length')}
            distribution={profile.sentence_length_distribution}
          />
          <TermList
            label={t('corpus.profile.openers')}
            values={profile.opener_patterns ?? []}
          />
          <TermList
            label={t('corpus.profile.hedges')}
            values={profile.hedging_patterns ?? []}
          />
          <TermList
            label={t('corpus.profile.common_terms')}
            values={profile.common_domain_terms ?? []}
          />
        </div>
      ) : (
        <p className="mt-3 leading-7 text-slate-700">
          {t('corpus.profile.empty')}
        </p>
      )}
    </aside>
  );
}

/**
 * PR-B2 diagnostics surface — directly answers the user's
 * "是不是假的？" trust question by showing detected language,
 * document/token counts, and any empty-section warnings the
 * backend recorded. Hidden when the profile predates PR-B2 (no
 * diagnostic fields).
 */
function ProfileDiagnostics({ profile }: { profile: StyleProfileSummary }) {
  const t = useT();
  const lang = profile.detected_language;
  const hasDiagnostics =
    lang !== undefined ||
    profile.document_count !== undefined ||
    profile.total_token_count !== undefined ||
    (profile.empty_section_warnings?.length ?? 0) > 0;
  if (!hasDiagnostics) return null;

  const langLabel = (() => {
    if (!lang) return null;
    const key = `corpus.profile.language.${lang}`;
    const translated = t(key);
    return translated === key ? lang : translated;
  })();

  return (
    <div
      className="grid gap-2 rounded-md border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700"
      data-testid="corpus-profile-diagnostics"
    >
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {langLabel ? (
          <span>
            <strong>{t('corpus.profile.detected_language')}:</strong> {langLabel}
          </span>
        ) : null}
        {profile.document_count !== undefined ? (
          <span>
            <strong>{t('corpus.profile.document_count')}:</strong>{' '}
            {profile.document_count}
          </span>
        ) : null}
        {profile.total_token_count !== undefined ? (
          <span>
            <strong>{t('corpus.profile.total_token_count')}:</strong>{' '}
            {profile.total_token_count}
          </span>
        ) : null}
      </div>
      {(profile.empty_section_warnings ?? []).length > 0 ? (
        <div className="grid gap-1">
          <strong className="text-amber-700">
            {t('corpus.profile.warnings')}
          </strong>
          <ul className="m-0 grid list-disc gap-1 pl-4 text-amber-700">
            {(profile.empty_section_warnings ?? []).map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function DistributionRow({
  label,
  distribution
}: {
  label: string;
  distribution?: { mean?: number; p25?: number; p75?: number };
}) {
  return (
    <div className="rounded-lg border border-slate-200 p-3">
      <h3 className="font-bold text-slate-500">{label}</h3>
      <p className="mt-1 text-slate-800">
        mean {formatNumber(distribution?.mean)} · p25{' '}
        {formatNumber(distribution?.p25)} · p75 {formatNumber(distribution?.p75)}
      </p>
    </div>
  );
}

function TermList({ label, values }: { label: string; values: string[] }) {
  return (
    <div>
      <h3 className="mb-2 font-bold text-slate-500">{label}</h3>
      {values.length ? (
        <div className="flex flex-wrap gap-2">
          {values.slice(0, 12).map((value) => (
            <span
              className="rounded-full bg-slate-100 px-3 py-1 font-semibold text-slate-700"
              key={value}
            >
              {value}
            </span>
          ))}
        </div>
      ) : (
        <p className="text-slate-700">None yet.</p>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const t = useT();
  const isReady = status === 'extracted' || status === 'profiled';
  const label = status === 'profiled' ? t('corpus.profiled') : status;
  return (
    <span
      className={`inline-flex rounded-full px-3 py-1 text-xs font-bold ${
        isReady ? 'bg-[#236b45]/10 text-[#236b45]' : 'bg-slate-100 text-slate-700'
      }`}
    >
      {label}
    </span>
  );
}

function formatBytes(value: number | null): string {
  if (value === null) {
    return 'unknown';
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleDateString();
}

function formatNumber(value: number | undefined): string {
  if (typeof value !== 'number') {
    return '0';
  }
  return value.toFixed(1);
}
