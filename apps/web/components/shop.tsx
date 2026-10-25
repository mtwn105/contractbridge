"use client";
import { useEffect, useRef, useState } from "react";
import { Icon } from "./icon";
import { ProductArt } from "./product-art";
import "@/app/shop.css";

type Product = { sku: string; name: string; description: string; price: number; color: string };
type Receipt = { orderId: string; status: string; totalAmount: number };
const dollars = (amount: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(amount / 100);

export default function Shop() {
  const [products, setProducts] = useState<Product[]>([]);
  const [cart, setCart] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [catalogError, setCatalogError] = useState("");
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [cartReady, setCartReady] = useState(false);
  const sending = useRef(false);
  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("fieldwork-cart-v1") || "{}");
      if (saved && typeof saved === "object" && !Array.isArray(saved)) {
        setCart(Object.fromEntries(Object.entries(saved).filter(([sku, amount]) =>
          ["sku-blue", "sku-red", "sku-green"].includes(sku) && Number.isInteger(amount) && Number(amount) >= 0 && Number(amount) <= 10)) as Record<string, number>);
      }
    } catch { /* A missing or malformed local cart starts empty. */ }
    setCartReady(true);
  }, []);
  useEffect(() => {
    if (cartReady) { try { localStorage.setItem("fieldwork-cart-v1", JSON.stringify(cart)); } catch { /* Storage is optional. */ } }
  }, [cart, cartReady]);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/shop/catalog", { signal: controller.signal, cache: "no-store" }).then(async r => {
      const data = await r.json(); if (!r.ok) throw new Error(data.title); setProducts(data.products);
    }).catch(e => { if (e.name !== "AbortError") setCatalogError(e.message || "The shop is unavailable."); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);
  const items = products.filter(p => cart[p.sku] > 0);
  const count = items.reduce((n, p) => n + cart[p.sku], 0);
  const total = items.reduce((n, p) => n + p.price * cart[p.sku], 0);
  function quantity(sku: string, next: number) {
    if (sending.current) return;
    setCart(current => ({ ...current, [sku]: Math.max(0, Math.min(10, next)) }));
    setReceipt(null); setError("");
  }
  async function checkout() {
    if (sending.current || !count) return;
    sending.current = true; setBusy(true); setError(""); setReceipt(null);
    try {
      const r = await fetch("/api/shop/checkout", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: items.map(p => ({ sku: p.sku, quantity: cart[p.sku] })) }) });
      const data = await r.json(); if (!r.ok) throw new Error(data.title || "Checkout is unavailable. Your cart is saved.");
      setReceipt(data);
    } catch (e) { setError((e as Error).message); }
    finally { sending.current = false; setBusy(false); }
  }
  return <div className="simple-shell store">
    <header className="simple-header"><a href="/shop" className="simple-brand"><span className="store-mark" aria-hidden="true">f.</span>Fieldwork Supply</a><nav aria-label="Main"><a href="/shop" aria-current="page">Example shop</a><a href="/">ContractBridge</a></nav></header>
    <main className="store-main"><div className="store-intro"><div><h1>A little order<br />for your everyday.</h1><p>Thoughtful essentials for your desk, your bag,<br className="store-desktop" /> and whatever you’re working on next.</p></div><span className="store-edition">The everyday collection<br /><strong>Three essentials. $9.99 each.</strong></span></div>
      <div className="store-layout"><section className="store-products" aria-label="Products">{catalogError ? <p role="alert">{catalogError}</p> : loading ? <p role="status">Opening the shop…</p> : products.map(p => <article className={`store-product ${p.color}`} key={p.sku}><div className="store-product-art"><ProductArt color={p.color} /></div><div className="store-product-heading"><h2>{p.name}</h2><span>{dollars(p.price)}</span></div><p>{p.description}</p><button className="shop-button shop-secondary" disabled={busy || (cart[p.sku] || 0) >= 10} onClick={() => quantity(p.sku, (cart[p.sku] || 0) + 1)}>Add to bag<span aria-hidden="true">+</span></button></article>)}</section>
        <aside className="store-cart" aria-labelledby="bag-title"><div className="store-bag-title"><h2 id="bag-title">Your bag</h2><span aria-label={`${count} items`}>{count}</span></div>{items.length ? <><ul className="store-cart-items">{items.map(p => <li key={p.sku}><div className={`store-cart-art ${p.color}`}><ProductArt color={p.color} /></div><div className="store-cart-line"><h3>{p.name}</h3><span>{dollars(p.price * cart[p.sku])}</span><div className="store-quantity"><button aria-label={`Remove one ${p.name}`} disabled={busy} onClick={() => quantity(p.sku, cart[p.sku] - 1)}>−</button><output aria-label={`${p.name} quantity`}>{cart[p.sku]}</output><button aria-label={`Add one ${p.name}`} disabled={busy || cart[p.sku] >= 10} onClick={() => quantity(p.sku, cart[p.sku] + 1)}>+</button></div></div></li>)}</ul><div className="store-total"><span>Total</span><strong>{dollars(total)}</strong></div></> : <p className="store-empty">A good place to start.<br />Add something you’ll use every day.</p>}
          <button className="shop-button shop-primary" disabled={!count || busy} onClick={checkout}>{busy ? "Placing your order…" : receipt ? "Place another order" : "Place order"}</button>
          {error ? <div className="store-alert" role="alert"><strong>We couldn’t complete checkout.</strong><p>{error}</p></div> : null}
          {receipt ? <div className="store-receipt" role="status"><span className="store-check" aria-hidden="true"><Icon name="check" size={14} /></span><h3>Your order is confirmed.</h3><p>Order <strong>{receipt.orderId}</strong><br />{dollars(receipt.totalAmount)} · Your bag is ready if you’d like to order again.</p></div> : null}
          <p className="store-demo-note">Demo store. No payment is collected.</p></aside></div>
    </main><footer className="simple-footer"><span>Fieldwork Supply</span><span>Small things, well considered.</span></footer>
  </div>;
}
