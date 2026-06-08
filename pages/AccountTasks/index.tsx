import {
  useState,
  useRef,
  useEffect,
  useCallback,
  memo,
  forwardRef,
  useImperativeHandle,
} from 'react';

import Tooltip from '@/components/Base/Tooltip';

declare const window: Window & { axios: any };

// ── Constants ────────────────────────────────────────────────────────────
const PAGE_SIZE = 50;
const REFRESH_SECONDS = 30;

type TaskState = 'pending' | 'completed' | 'canceled';
type StateFilter = TaskState | 'all';

interface TaskRow {
  id: number;
  type: string;
  account_number: string;
  value: number;
  notes: string;
  customer_id: number | null;
  customer_name: string;
  state: TaskState;
  date: string | null;
  time: string | null;
}

interface ApiResponse {
  results: TaskRow[];
  page: number;
  num_pages: number;
  total: number;
  page_size: number;
  has_next: boolean;
  has_prev: boolean;
}

// ── Inline icons (no external dep — extension pages can't rely on lucide) ────
const I = {
  copy: (c = '') => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={c}>
      <rect x="9" y="9" width="13" height="13" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  ),
  check: (c = '') => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className={c}>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  ),
  x: (c = '') => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className={c}>
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  ),
  refresh: (c = '') => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={c}>
      <path d="M3 12a9 9 0 0 1 15-6.7L21 8" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-15 6.7L3 16" />
      <path d="M3 21v-5h5" />
    </svg>
  ),
  search: (c = '') => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={c}>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  ),
};

// ── Isolated countdown — re-renders only itself, never the list ─────────────
interface TimerHandle {
  reset: () => void;
}
const RefreshTimer = forwardRef<TimerHandle, { onFire: () => void }>(
  function RefreshTimer({ onFire }, ref) {
    const [remaining, setRemaining] = useState(REFRESH_SECONDS);
    const onFireRef = useRef(onFire);
    useEffect(() => {
      onFireRef.current = onFire;
    }, [onFire]);
    useImperativeHandle(ref, () => ({ reset: () => setRemaining(REFRESH_SECONDS) }), []);
    useEffect(() => {
      const t = setInterval(() => {
        setRemaining((r) => {
          if (r <= 1) {
            onFireRef.current?.();
            return REFRESH_SECONDS;
          }
          return r - 1;
        });
      }, 1000);
      return () => clearInterval(t);
    }, []);
    return <span className="tabular-nums">{remaining}</span>;
  },
);

// ── Click-to-copy number — the value itself is the copy target.
// Copying also FOCUSES (selects) the card, so the operator never has to click
// the card first. Hover shows a tooltip hint.
function CopyNumber({
  raw,
  display,
  accent,
  tip,
  onCopied,
}: {
  raw: string;
  display: string;
  accent: string;
  tip: string;
  onCopied: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const tRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copy = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      try {
        navigator.clipboard.writeText(raw);
      } catch {
        /* ignore */
      }
      onCopied(); // focus the card on copy
      setCopied(true);
      if (tRef.current) clearTimeout(tRef.current);
      tRef.current = setTimeout(() => setCopied(false), 1100);
    },
    [raw, onCopied],
  );
  useEffect(() => () => { if (tRef.current) clearTimeout(tRef.current); }, []);

  return (
    <Tooltip label={copied ? 'تم النسخ' : tip} position="top">
      <button
        type="button"
        onClick={copy}
        className={`group flex min-w-[7rem] items-center justify-between gap-3 rounded-lg border px-3 py-2 text-right transition-colors ${
          copied ? 'border-emerald-300 bg-emerald-50' : 'border-transparent bg-white/70 hover:bg-white'
        }`}
      >
        <span dir="ltr" className={`font-mono text-xl font-bold tabular-nums ${copied ? 'text-emerald-600' : accent}`}>
          {display}
        </span>
        {copied
          ? I.check('h-4 w-4 shrink-0 text-emerald-500')
          : I.copy('h-4 w-4 shrink-0 text-gray-300 group-hover:text-blue-400')}
      </button>
    </Tooltip>
  );
}

// ── Task card (memoized; keyed by id so local state survives refresh) ───────
interface CardProps {
  row: TaskRow;
  selected: boolean;
  showState: boolean;
  onSelect: (id: number) => void;
  onPick: (id: number) => void;
  onComplete: (id: number) => Promise<void>;
  onCancel: (id: number) => Promise<void>;
}
const TaskCard = memo(function TaskCard({
  row,
  selected,
  showState,
  onSelect,
  onPick,
  onComplete,
  onCancel,
}: CardProps) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const amount = Math.round(row.value);
  const run = useCallback(
    async (fn: (id: number) => Promise<void>, e: React.MouseEvent) => {
      e.stopPropagation();
      if (busy) return;
      setBusy(true);
      try {
        await fn(row.id);
      } finally {
        setBusy(false);
        setConfirming(false);
      }
    },
    [busy, row.id],
  );

  const isFawry = row.type === 'فورى';
  // Whole-card tint by type: فورى = yellow, أمان = blue.
  const tint = isFawry ? 'bg-amber-50 border-amber-300' : 'bg-sky-50 border-sky-300';
  const pending = row.state === 'pending';
  // Focus (ring + "يتم التحويل") only makes sense for in-progress work.
  const focused = selected && pending;

  return (
    <div
      onClick={() => pending && onSelect(row.id)}
      className={`relative rounded-xl border px-3 py-3 transition-all ${tint} ${
        pending ? 'cursor-pointer hover:brightness-[0.98]' : 'cursor-default opacity-75'
      } ${focused ? 'ring-2 ring-blue-500 ring-offset-1' : ''}`}
    >
      {/* Quiet context line: type · customer · time  (+ optional state) */}
      <div className="mb-2 flex items-center gap-1.5 text-xs text-gray-400">
        <span className={isFawry ? 'font-semibold text-amber-600' : 'font-semibold text-violet-600'}>
          {row.type}
        </span>
        <span>·</span>
        <span className="truncate text-gray-500">{row.customer_name}</span>
        {row.time && (
          <>
            <span>·</span>
            <span className="shrink-0">{row.time}</span>
          </>
        )}
        {/* trailing badges — pushed to the end */}
        <span className="mr-auto" />
        {focused && (
          <span className="shrink-0 rounded px-1.5 py-0.5 font-semibold bg-blue-100 text-blue-700">
            يتم التحويل
          </span>
        )}
        {showState && !pending && (
          <span
            className={`shrink-0 rounded px-1.5 py-0.5 font-medium ${
              row.state === 'completed' ? 'bg-emerald-50 text-emerald-600' : 'bg-red-50 text-red-600'
            }`}
          >
            {row.state === 'completed' ? 'تم' : 'ملغي'}
          </span>
        )}
      </div>

      {/* Heroes: account number + amount (click to copy + focus) + actions */}
      <div className="flex items-center gap-2">
        <CopyNumber
          raw={row.account_number}
          display={row.account_number || '—'}
          accent="text-gray-800"
          tip="نسخ رقم الحساب"
          onCopied={() => pending && onPick(row.id)}
        />
        <CopyNumber
          raw={String(amount)}
          display={amount.toLocaleString('en-US')}
          accent="text-emerald-600"
          tip="نسخ المبلغ"
          onCopied={() => pending && onPick(row.id)}
        />

        {pending && (
          <div className="ms-auto flex shrink-0 items-center gap-1.5">
            <Tooltip label="تم التحويل" position="top">
              <button
                type="button"
                onClick={(e) => run(onComplete, e)}
                disabled={busy}
                className="grid h-9 w-9 place-items-center rounded-lg bg-emerald-500 text-white hover:bg-emerald-600 disabled:opacity-50"
              >
                {I.check('h-5 w-5')}
              </button>
            </Tooltip>

            {/* Cancel — opens a tooltip-style confirmation bubble */}
            <div className="relative">
              <Tooltip label="إلغاء العملية" position="top">
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setConfirming((c) => !c);
                  }}
                  className={`grid h-9 w-9 place-items-center rounded-lg transition-colors ${
                    confirming
                      ? 'bg-red-100 text-red-700'
                      : 'bg-gray-100 text-gray-400 hover:bg-red-100 hover:text-red-600'
                  }`}
                >
                  {I.x('h-4 w-4')}
                </button>
              </Tooltip>

              {confirming && (
                <>
                  {/* click-away backdrop */}
                  <div
                    className="fixed inset-0 z-10"
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirming(false);
                    }}
                  />
                  <div
                    onClick={(e) => e.stopPropagation()}
                    className="absolute bottom-full left-0 z-20 mb-2 w-max rounded-lg bg-white p-2 text-right shadow-xl ring-1 ring-gray-200"
                  >
                    <div className="mb-1.5 px-1 text-xs font-medium text-gray-700">إلغاء العملية؟</div>
                    <div className="flex gap-1.5">
                      <button
                        type="button"
                        onClick={(e) => run(onCancel, e)}
                        disabled={busy}
                        className="rounded-md bg-red-600 px-3 py-1 text-xs font-semibold text-white hover:bg-red-700 disabled:opacity-50"
                      >
                        نعم، إلغاء
                      </button>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setConfirming(false);
                        }}
                        className="rounded-md bg-gray-100 px-3 py-1 text-xs text-gray-600 hover:bg-gray-200"
                      >
                        تراجع
                      </button>
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
});

// ── Segmented control ───────────────────────────────────────────────────────
function Segment<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { v: T; label: React.ReactNode }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex rounded-lg bg-gray-100 p-0.5">
      {options.map((o) => (
        <button
          key={o.v}
          type="button"
          onClick={() => onChange(o.v)}
          className={`rounded-md px-3 py-1 text-sm font-medium transition-colors ${
            value === o.v ? 'bg-white text-gray-800 shadow-sm' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────
export default function AccountTasks() {
  const [rows, setRows] = useState<TaskRow[]>([]);
  const [meta, setMeta] = useState({ page: 1, num_pages: 1, total: 0 });
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [stateFilter, setStateFilter] = useState<StateFilter>('pending');
  const [accountType, setAccountType] = useState<string>('');
  const [searchInput, setSearchInput] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [page, setPage] = useState(1);

  const timerRef = useRef<TimerHandle>(null);
  const reqIdRef = useRef(0);
  const stateFilterRef = useRef(stateFilter);
  useEffect(() => {
    stateFilterRef.current = stateFilter;
  }, [stateFilter]);

  const fetchRows = useCallback(
    async (opts?: { silent?: boolean }) => {
      const reqId = ++reqIdRef.current;
      if (!opts?.silent) setLoading(true);
      try {
        const params: Record<string, string | number> = { state: stateFilter, page, page_size: PAGE_SIZE };
        if (accountType) params.account_type = accountType;
        if (appliedSearch) params.search = appliedSearch;
        const resp = await window.axios.get('/qurtoba/api/account-tasks/', { params });
        if (reqId !== reqIdRef.current) return;
        const data = resp.data as ApiResponse;
        setRows(data.results || []);
        setMeta({ page: data.page, num_pages: data.num_pages, total: data.total });
        setError(null);
      } catch (e: any) {
        if (reqId !== reqIdRef.current) return;
        setError(e?.response?.data?.error || 'فشل تحميل البيانات');
      } finally {
        if (reqId === reqIdRef.current && !opts?.silent) setLoading(false);
      }
    },
    [stateFilter, accountType, appliedSearch, page],
  );

  useEffect(() => {
    fetchRows();
    timerRef.current?.reset();
  }, [fetchRows]);

  useEffect(() => {
    const t = setTimeout(() => {
      setPage(1);
      setAppliedSearch(searchInput.trim());
    }, 350);
    return () => clearTimeout(t);
  }, [searchInput]);

  const handleSelect = useCallback((id: number) => {
    setSelectedId((cur) => (cur === id ? null : id));
  }, []);

  // Force-select (used when copying a value) — never toggles off.
  const handlePick = useCallback((id: number) => setSelectedId(id), []);

  const applyLocalState = useCallback((id: number, newState: TaskState) => {
    const filter = stateFilterRef.current;
    const drops = filter !== 'all' && filter !== newState;
    setRows((prev) =>
      drops ? prev.filter((r) => r.id !== id) : prev.map((r) => (r.id === id ? { ...r, state: newState } : r)),
    );
    if (drops) setMeta((m) => ({ ...m, total: Math.max(0, m.total - 1) }));
  }, []);

  const handleComplete = useCallback(
    async (id: number) => {
      try {
        await window.axios.post(`/qurtoba/api/account-tasks/${id}/complete/`);
        applyLocalState(id, 'completed');
      } catch {
        setError('تعذّر تنفيذ العملية');
      }
    },
    [applyLocalState],
  );

  const handleCancel = useCallback(
    async (id: number) => {
      try {
        await window.axios.post(`/qurtoba/api/account-tasks/${id}/cancel/`);
        applyLocalState(id, 'canceled');
      } catch {
        setError('تعذّر تنفيذ العملية');
      }
    },
    [applyLocalState],
  );

  const manualRefresh = useCallback(() => {
    fetchRows();
    timerRef.current?.reset();
  }, [fetchRows]);

  const silentRefresh = useCallback(() => fetchRows({ silent: true }), [fetchRows]);

  const canPrev = page > 1;
  const canNext = page < meta.num_pages;
  const showState = stateFilter === 'all';

  return (
    <div dir="rtl" className="mx-auto max-w-2xl px-4 py-5">
      <style>{`.at-search::placeholder{opacity:.45}`}</style>
      {/* Header */}
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-baseline gap-2">
          <h1 className="text-xl font-bold text-gray-800">فورى / أمان</h1>
          <span className="text-sm text-gray-400">{meta.total}</span>
        </div>
        <button
          type="button"
          onClick={manualRefresh}
          title="تحديث الآن"
          className="flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-500 hover:bg-gray-50"
        >
          {I.refresh('h-3.5 w-3.5')}
          <RefreshTimer ref={timerRef} onFire={silentRefresh} />
        </button>
      </div>

      {/* Filters — two labeled groups (الحالة / النوع) + search */}
      <div className="mb-4 rounded-xl border border-gray-200 bg-white p-3">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          {/* Filter 1 — الحالة */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-gray-400">الحالة</span>
            <Segment
              value={stateFilter}
              onChange={setStateFilter}
              options={[
                { v: 'pending', label: 'قيد التنفيذ' },
                { v: 'completed', label: 'تم' },
                { v: 'canceled', label: 'ملغي' },
                { v: 'all', label: 'الكل' },
              ]}
            />
          </div>

          <span className="hidden h-6 w-px bg-gray-200 sm:block" />

          {/* Filter 2 — النوع */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-gray-400">النوع</span>
            <Segment
              value={accountType}
              onChange={setAccountType}
              options={[
                { v: '', label: 'الكل' },
                {
                  v: 'فورى',
                  label: (
                    <span className="flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-full bg-amber-400" />
                      فورى
                    </span>
                  ),
                },
                {
                  v: 'أمان',
                  label: (
                    <span className="flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-full bg-sky-400" />
                      أمان
                    </span>
                  ),
                },
              ]}
            />
          </div>

          {/* Search */}
          <div className="relative ms-auto">
            <span className="pointer-events-none absolute inset-y-0 right-2.5 flex items-center text-gray-300">
              {I.search('h-4 w-4')}
            </span>
            <input
              type="text"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="بحث برقم الحساب / العميل / المبلغ"
              className="at-search w-48 rounded-lg border border-gray-200 bg-gray-50 py-1.5 pr-8 pl-3 text-right text-sm transition-all focus:w-64 focus:border-blue-300 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-200"
            />
          </div>
        </div>
      </div>

      {error && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>
      )}

      {/* List */}
      <div className="space-y-2">
        {rows.map((row) => (
          <TaskCard
            key={row.id}
            row={row}
            selected={selectedId === row.id}
            showState={showState}
            onSelect={handleSelect}
            onPick={handlePick}
            onComplete={handleComplete}
            onCancel={handleCancel}
          />
        ))}
        {rows.length === 0 && !loading && (
          <div className="py-16 text-center text-sm text-gray-300">لا توجد عمليات</div>
        )}
        {loading && rows.length === 0 && (
          <div className="py-16 text-center text-sm text-gray-300">جارٍ التحميل...</div>
        )}
      </div>

      {/* Pagination */}
      {meta.num_pages > 1 && (
        <div className="mt-5 flex items-center justify-center gap-3">
          <button
            type="button"
            disabled={!canPrev}
            onClick={() => canPrev && setPage((p) => p - 1)}
            className="grid h-9 w-9 place-items-center rounded-lg border border-gray-200 bg-white text-gray-500 hover:bg-gray-50 disabled:opacity-30"
          >
            ←
          </button>
          <span className="text-sm text-gray-400">
            {meta.page} / {meta.num_pages}
          </span>
          <button
            type="button"
            disabled={!canNext}
            onClick={() => canNext && setPage((p) => p + 1)}
            className="grid h-9 w-9 place-items-center rounded-lg border border-gray-200 bg-white text-gray-500 hover:bg-gray-50 disabled:opacity-30"
          >
            →
          </button>
        </div>
      )}
    </div>
  );
}
