import { usePage } from '@inertiajs/react';
import { useState } from 'react';

declare const window: Window & { axios: any };

interface DelayedRow {
  id: number;
  customer: {
    id: number;
    name: string;
    deviceNo: number;
    seller: { id: number; name: string } | null;
  };
  value: number;
  date: string;
  time: string;
  days: number;
}

interface Seller {
  id: number;
  name: string;
}

interface Props {
  rows: DelayedRow[];
  sellers: Seller[];
  filters: Record<string, string>;
  error: string | null;
}

export default function DelayedCustomers() {
  const { props } = usePage();
  const { rows: initialRows, sellers, filters: initialFilters, error: initialError } = props as unknown as Props;

  const [rows, setRows] = useState<DelayedRow[]>(initialRows || []);
  const [sellerId, setSellerId] = useState(initialFilters?.['customer__seller__id'] || '');
  const [minValue, setMinValue] = useState(initialFilters?.gte_value || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(initialError || null);

  const applyFilters = async () => {
    setLoading(true);
    setError(null);
    try {
      const params: Record<string, string> = { ordering: '-days' };
      if (sellerId) params['customer__seller__id'] = sellerId;
      if (minValue) params.gte_value = minValue;
      const resp = await window.axios.get('/qurtoba/api/delayed/', { params });
      const data = resp.data;
      setRows(Array.isArray(data) ? data : (data?.results || []));
    } catch (e: any) {
      setError(e?.response?.data?.error || 'فشل الاتصال بقرطبة');
    } finally {
      setLoading(false);
    }
  };

  const daysClass = (days: number) => {
    if (days > 60) return 'bg-red-600 text-white';
    if (days > 30) return 'bg-red-100 text-red-700';
    if (days > 14) return 'bg-orange-100 text-orange-700';
    return 'bg-gray-100 text-gray-600';
  };

  const rowClass = (days: number) => {
    if (days > 30) return 'bg-red-50 hover:bg-red-100';
    return 'hover:bg-gray-50';
  };

  return (
    <div dir="rtl" className="p-4 space-y-4">
      <h1 className="text-2xl font-bold text-gray-800">المتأخرات</h1>

      {/* Summary strip */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-white rounded-xl border border-gray-200 px-4 py-3 text-center">
          <div className="text-2xl font-bold text-gray-800">{rows.length}</div>
          <div className="text-sm text-gray-400 mt-1">إجمالى العملاء</div>
        </div>
        <div className="bg-red-50 rounded-xl border border-red-200 px-4 py-3 text-center">
          <div className="text-2xl font-bold text-red-600">{rows.filter(r => r.days > 30).length}</div>
          <div className="text-sm text-red-400 mt-1">أكثر من 30 يوم</div>
        </div>
        <div className="bg-white rounded-xl border border-gray-200 px-4 py-3 text-center">
          <div className="text-2xl font-bold text-emerald-600">
            {rows.reduce((s, r) => s + (r.value || 0), 0).toLocaleString('ar-EG')}
          </div>
          <div className="text-sm text-gray-400 mt-1">إجمالى المبالغ</div>
        </div>
      </div>

      {/* Filters */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 flex flex-wrap gap-3 items-end">
        <div className="flex flex-col gap-1">
          <label className="text-sm text-gray-500">المندوب</label>
          <select
            value={sellerId}
            onChange={e => setSellerId(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 w-44 text-right focus:outline-none focus:ring-2 focus:ring-blue-400"
          >
            <option value="">كل المناديب</option>
            {sellers.map(s => (
              <option key={s.id} value={String(s.id)}>{s.name}</option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-sm text-gray-500">الحد الأدنى للمبلغ</label>
          <input
            type="number"
            value={minValue}
            onChange={e => setMinValue(e.target.value)}
            placeholder="0"
            className="border border-gray-300 rounded-lg px-3 py-2 w-36 text-right focus:outline-none focus:ring-2 focus:ring-blue-400"
          />
        </div>
        <button
          onClick={applyFilters}
          disabled={loading}
          className="px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors disabled:opacity-50"
        >
          {loading ? 'جارى البحث...' : 'بحث'}
        </button>
      </div>

      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-red-700 text-sm">{error}</div>
      )}

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              {['اسم العميل', 'كود', 'المندوب', 'المبلغ', 'آخر معاملة', 'عدد الأيام'].map(h => (
                <th key={h} className="px-3 py-3 text-right font-medium text-gray-600">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr key={row.id} className={`border-b border-gray-100 last:border-0 transition-colors ${rowClass(row.days)}`}>
                <td className="px-3 py-3 font-medium text-gray-800">{row.customer?.name}</td>
                <td className="px-3 py-3 text-gray-400">{row.customer?.deviceNo}</td>
                <td className="px-3 py-3 text-gray-500">{row.customer?.seller?.name || '—'}</td>
                <td className="px-3 py-3 font-semibold text-emerald-600">
                  {(row.value || 0).toLocaleString('ar-EG')}
                </td>
                <td className="px-3 py-3 text-gray-500">{row.date}</td>
                <td className="px-3 py-3">
                  <span className={`px-2 py-1 rounded-full text-xs font-bold ${daysClass(row.days)}`}>
                    {row.days} يوم
                  </span>
                </td>
              </tr>
            ))}
            {rows.length === 0 && !loading && (
              <tr>
                <td colSpan={6} className="text-center py-8 text-gray-400">لا توجد بيانات</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
