import { usePage } from '@inertiajs/react';
import { useState } from 'react';

declare const window: Window & { axios: any };

interface ReportRow {
  type: string;
  value__sum: number;
}

interface Props {
  rows: ReportRow[];
  df: string;
  dt: string;
  error: string | null;
}

const YELLOW_TYPES = new Set(['المتبقى', 'المتبقى مندوب', 'إجمالى المتبقى']);
const TEAL_TYPES = new Set(['إجمالى التحويلات']);

export default function AccountantReport() {
  const { props } = usePage();
  const { rows: initialRows, df: initialDf, dt: initialDt, error: initialError } = props as unknown as Props;

  const [rows, setRows] = useState<ReportRow[]>(initialRows || []);
  const [df, setDf] = useState(initialDf || '');
  const [dt, setDt] = useState(initialDt || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(initialError || null);

  const applyFilters = async () => {
    if (!df || !dt) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await window.axios.get('/qurtoba/api/accountant-report/', { params: { df, dt } });
      setRows(resp.data?.data || []);
    } catch (e: any) {
      setError(e?.response?.data?.error || 'فشل الاتصال بقرطبة');
    } finally {
      setLoading(false);
    }
  };

  const handleShare = () => {
    const text = rows.map(r => `${r.type}: ${(r.value__sum || 0).toLocaleString('ar-EG')}`).join('\n');
    if (navigator.share) {
      navigator.share({ title: 'تقرير المحاسب', text });
    } else {
      navigator.clipboard.writeText(text);
    }
  };

  const rowClass = (type: string) => {
    if (YELLOW_TYPES.has(type)) return 'bg-yellow-50 font-bold text-gray-800';
    if (TEAL_TYPES.has(type)) return 'bg-teal-700 text-white font-bold';
    return 'hover:bg-gray-50';
  };

  const valueClass = (type: string, value: number) => {
    if (TEAL_TYPES.has(type)) return 'text-white font-bold';
    if (YELLOW_TYPES.has(type)) return value > 0 ? 'text-emerald-600 font-bold' : 'text-red-600 font-bold';
    return value > 0 ? 'text-gray-800' : 'text-red-500';
  };

  return (
    <div dir="rtl" className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-800">تقارير المحاسب</h1>
        <button
          onClick={handleShare}
          className="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-50 transition-colors"
        >
          مشاركة
        </button>
      </div>

      {/* Date range filter */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 flex flex-wrap gap-3 items-end">
        <div className="flex flex-col gap-1">
          <label className="text-sm text-gray-500">من</label>
          <input type="date" value={df} onChange={e => setDf(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-400" />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-sm text-gray-500">إلى</label>
          <input type="date" value={dt} onChange={e => setDt(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-400" />
        </div>
        <button onClick={applyFilters} disabled={loading || !df || !dt}
          className="px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors disabled:opacity-50">
          {loading ? 'جارى التحميل...' : 'عرض'}
        </button>
      </div>

      {/* Date shown */}
      {df && dt && (
        <div className="text-sm text-gray-400">
          الفترة: {df} إلى {dt}
        </div>
      )}

      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-red-700 text-sm">{error}</div>
      )}

      {/* Report table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="px-4 py-3 text-right font-medium text-gray-600">النوع</th>
              <th className="px-4 py-3 text-left font-medium text-gray-600">المبلغ</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className={`border-b border-gray-100 last:border-0 transition-colors ${rowClass(row.type)}`}>
                <td className="px-4 py-3">{row.type}</td>
                <td className={`px-4 py-3 text-left font-mono ${valueClass(row.type, row.value__sum)}`}>
                  {(row.value__sum || 0).toLocaleString('ar-EG')}
                </td>
              </tr>
            ))}
            {rows.length === 0 && !loading && (
              <tr>
                <td colSpan={2} className="text-center py-8 text-gray-400">اختر فترة زمنية وانقر عرض</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
