import { usePage } from '@inertiajs/react';
import { useState } from 'react';

declare const window: Window & { axios: any };

interface DuesRow {
  deviceNo: number;
  customerName: string;
  id_customer: number;
  rest: number;
  seller: string;
  phoneNo?: string;
}

interface Seller {
  id: number;
  name: string;
}

interface Props {
  rows: DuesRow[];
  sellers: Seller[];
  filters: Record<string, string>;
  error: string | null;
}

export default function CustomerDues() {
  const { props } = usePage();
  const { rows: initialRows, sellers, filters: initialFilters, error: initialError } = props as unknown as Props;

  const [rows, setRows] = useState<DuesRow[]>(initialRows || []);
  const [gte, setGte] = useState(initialFilters?.gte || '');
  const [repo, setRepo] = useState(initialFilters?.repo || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(initialError || null);

  const total = rows.filter(r => r.deviceNo !== 0).reduce((s, r) => s + (r.rest || 0), 0);

  const applyFilters = async () => {
    setLoading(true);
    setError(null);
    try {
      const params: Record<string, string> = {};
      if (gte) params.gte = gte;
      if (repo) params.repo = repo;
      const resp = await window.axios.get('/qurtoba/api/customer-dues/', { params });
      setRows(resp.data?.data || []);
    } catch (e: any) {
      setError(e?.response?.data?.error || 'فشل الاتصال بقرطبة');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div dir="rtl" className="p-4 space-y-4 font-arabic">
      {/* Header */}
      <h1 className="text-2xl font-bold text-gray-800">مستحقات العملاء</h1>

      {/* Total */}
      <div className="rounded-xl bg-emerald-50 border border-emerald-200 px-6 py-4 flex items-center justify-between">
        <span className="text-gray-600 font-medium">إجمالى المستحقات</span>
        <span className={`text-2xl font-bold ${total >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>
          {total.toLocaleString('ar-EG')}
        </span>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-end bg-white rounded-xl border border-gray-200 p-4">
        <div className="flex flex-col gap-1">
          <label className="text-sm text-gray-500">الحد الأدنى للرصيد</label>
          <input
            type="number"
            value={gte}
            onChange={e => setGte(e.target.value)}
            placeholder="0"
            className="border border-gray-300 rounded-lg px-3 py-2 w-36 text-right focus:outline-none focus:ring-2 focus:ring-emerald-400"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-sm text-gray-500">المندوب</label>
          <select
            value={repo}
            onChange={e => setRepo(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 w-44 text-right focus:outline-none focus:ring-2 focus:ring-emerald-400"
          >
            <option value="">كل المناديب</option>
            {sellers.map(s => (
              <option key={s.id} value={String(s.id)}>{s.name}</option>
            ))}
          </select>
        </div>
        <button
          onClick={applyFilters}
          disabled={loading}
          className="px-5 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-medium transition-colors disabled:opacity-60"
        >
          {loading ? 'جارى البحث...' : 'بحث'}
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-red-700 text-sm">
          {error}
        </div>
      )}

      {/* Rows */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {rows.length === 0 && !loading && (
          <div className="p-8 text-center text-gray-400">لا توجد بيانات</div>
        )}
        {rows.map((row, i) =>
          row.deviceNo === 0 ? (
            // Area header
            <div key={i} className="bg-orange-500 text-white px-4 py-2 font-bold text-sm">
              {row.customerName}
            </div>
          ) : (
            // Customer row
            <div
              key={i}
              className="flex items-center justify-between px-4 py-3 border-b border-gray-100 last:border-0 hover:bg-gray-50 transition-colors"
            >
              <div className="flex-1">
                <div className="font-medium text-gray-800">{row.customerName}</div>
                {row.seller && <div className="text-xs text-gray-400">{row.seller}</div>}
              </div>
              <div className="text-gray-400 text-sm mx-4">{row.deviceNo}</div>
              <span
                className={`px-3 py-1 rounded-full text-sm font-semibold min-w-[70px] text-center ${
                  row.rest > 0
                    ? 'bg-emerald-100 text-emerald-700'
                    : row.rest < 0
                    ? 'bg-red-100 text-red-700'
                    : 'bg-gray-100 text-gray-500'
                }`}
              >
                {row.rest.toLocaleString('ar-EG')}
              </span>
            </div>
          )
        )}
      </div>
    </div>
  );
}
