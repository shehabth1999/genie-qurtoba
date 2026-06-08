import { usePage } from '@inertiajs/react';
import { useState } from 'react';

declare const window: Window & { axios: any };

interface SellerSummary {
  id: number;
  name: string;
  value: number;
  count: number;
}

interface Transaction {
  id: number;
  type: string;
  accountNumber: string;
  value: number;
  rest: number;
  isDone: boolean;
  isDown: boolean;
  isSeller: boolean;
  customerData: { id: number; name: string; deviceNo: number } | null;
  accountant: string;
  seller: number;
  rest_collector: number;
  date: string;
  time: string;
  notes: string | null;
}

interface Props {
  sellers: SellerSummary[];
  error: string | null;
}

export default function SellerDues() {
  const { props } = usePage();
  const { sellers: initialSellers, error: initialError } = props as unknown as Props;

  const [sellers, setSellers] = useState<SellerSummary[]>(initialSellers || []);
  const [selected, setSelected] = useState<SellerSummary | null>(null);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [settleValue, setSettleValue] = useState('');
  const [loading, setLoading] = useState(false);
  const [settleLoading, setSettleLoading] = useState(false);
  const [error, setError] = useState<string | null>(initialError || null);
  const [settleMsg, setSettleMsg] = useState('');

  const loadTransactions = async (seller: SellerSummary, from?: string, to?: string) => {
    setLoading(true);
    setError(null);
    setTransactions([]);
    try {
      const params: Record<string, string> = { id: String(seller.id) };
      if (from && to) { params.from = from; params.to = to; }
      const resp = await window.axios.get('/qurtoba/api/seller-transactions/', { params });
      setTransactions(resp.data?.result || []);
    } catch (e: any) {
      setError(e?.response?.data?.error || 'فشل تحميل البيانات');
    } finally {
      setLoading(false);
    }
  };

  const handleSelectSeller = (s: SellerSummary) => {
    setSelected(s);
    setDateFrom('');
    setDateTo('');
    setSettleMsg('');
    loadTransactions(s);
  };

  const handleDateFilter = () => {
    if (selected) loadTransactions(selected, dateFrom, dateTo);
  };

  const handleSettle = async () => {
    if (!selected || !settleValue) return;
    setSettleLoading(true);
    setSettleMsg('');
    try {
      const resp = await window.axios.post('/qurtoba/api/seller-settle/', {
        id: selected.id,
        value: settleValue,
      });
      setSettleMsg(resp.data?.message || 'تم بنجاح');
      setSettleValue('');
      // Refresh sellers list
      const r2 = await window.axios.get('/qurtoba/api/seller-dues/');
      setSellers(r2.data?.result || sellers);
      // Refresh transactions
      loadTransactions(selected);
    } catch (e: any) {
      setSettleMsg(e?.response?.data?.error || 'فشل العملية');
    } finally {
      setSettleLoading(false);
    }
  };

  // ------ Sellers list screen ------
  if (!selected) {
    return (
      <div dir="rtl" className="p-4 space-y-4">
        <h1 className="text-2xl font-bold text-gray-800">مستحقات المناديب</h1>
        {error && (
          <div className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-red-700 text-sm">{error}</div>
        )}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {sellers.map(s => (
            <button
              key={s.id}
              onClick={() => handleSelectSeller(s)}
              className="bg-white rounded-xl border border-gray-200 p-5 text-right hover:border-blue-400 hover:shadow-md transition-all group"
            >
              <div className="text-lg font-bold text-gray-800 group-hover:text-blue-600">{s.name}</div>
              <div className="mt-2 flex items-center justify-between">
                <span className="text-sm text-gray-400">{s.count} معاملة</span>
                <span className="text-xl font-bold text-blue-600">{(s.value || 0).toLocaleString('ar-EG')}</span>
              </div>
            </button>
          ))}
          {sellers.length === 0 && (
            <div className="col-span-3 text-center text-gray-400 py-12">لا توجد بيانات</div>
          )}
        </div>
      </div>
    );
  }

  // ------ Transactions drill-down screen ------
  return (
    <div dir="rtl" className="p-4 space-y-4">
      {/* Back + title */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => setSelected(null)}
          className="text-blue-600 hover:text-blue-800 text-sm font-medium"
        >
          ← العودة
        </button>
        <h1 className="text-2xl font-bold text-gray-800">{selected.name}</h1>
        <span className="text-gray-400 text-sm">({selected.count} معاملة)</span>
        <span className="mr-auto text-xl font-bold text-blue-600">
          {(selected.value || 0).toLocaleString('ar-EG')}
        </span>
      </div>

      {/* Filters + settle */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 flex flex-wrap gap-3 items-end">
        <div className="flex flex-col gap-1">
          <label className="text-sm text-gray-500">من</label>
          <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-400" />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-sm text-gray-500">إلى</label>
          <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-400" />
        </div>
        <button onClick={handleDateFilter} disabled={loading || !dateFrom || !dateTo}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors disabled:opacity-50">
          تصفية
        </button>
        <div className="mr-auto flex items-center gap-2">
          <input
            type="number"
            value={settleValue}
            onChange={e => setSettleValue(e.target.value)}
            placeholder="مبلغ السداد"
            className="border border-gray-300 rounded-lg px-3 py-2 w-36 text-right focus:outline-none focus:ring-2 focus:ring-emerald-400"
          />
          <button onClick={handleSettle} disabled={settleLoading || !settleValue}
            className="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-medium transition-colors disabled:opacity-50">
            {settleLoading ? '...' : 'سداد مبلغ'}
          </button>
        </div>
      </div>
      {settleMsg && (
        <div className={`rounded-lg px-4 py-2 text-sm ${settleMsg.includes('نجاح') || settleMsg.includes('تم') ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-red-50 text-red-700 border border-red-200'}`}>
          {settleMsg}
        </div>
      )}

      {/* Transactions table */}
      {loading ? (
        <div className="text-center py-8 text-gray-400">جارى التحميل...</div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                {['#', 'اسم المستفيد', 'كود', 'المبلغ', 'المتبقى', 'المسئول', 'المتبقى على المندوب', 'الوقت', 'التاريخ'].map(h => (
                  <th key={h} className="px-3 py-3 text-right font-medium text-gray-600">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {transactions.map((t, i) => (
                <tr key={t.id} className={`border-b border-gray-100 hover:bg-gray-50 ${i % 2 === 0 ? '' : 'bg-gray-50/50'}`}>
                  <td className="px-3 py-3 text-gray-400">{i + 1}</td>
                  <td className="px-3 py-3 font-medium">{t.customerData?.name || '—'}</td>
                  <td className="px-3 py-3 text-gray-500">{t.customerData?.deviceNo ?? '—'}</td>
                  <td className="px-3 py-3 font-semibold text-blue-600">{(t.value || 0).toLocaleString('ar-EG')}</td>
                  <td className="px-3 py-3">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${(t.rest || 0) > 0 ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-700'}`}>
                      {(t.rest || 0).toLocaleString('ar-EG')}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-gray-500">{t.accountant}</td>
                  <td className="px-3 py-3">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${(t.rest_collector || 0) > 0 ? 'bg-orange-100 text-orange-700' : 'bg-gray-100 text-gray-500'}`}>
                      {(t.rest_collector || 0).toLocaleString('ar-EG')}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-gray-400 text-xs">{t.time?.slice(0, 5)}</td>
                  <td className="px-3 py-3 text-gray-500">{t.date}</td>
                </tr>
              ))}
              {transactions.length === 0 && (
                <tr>
                  <td colSpan={9} className="text-center py-8 text-gray-400">لا توجد معاملات</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
