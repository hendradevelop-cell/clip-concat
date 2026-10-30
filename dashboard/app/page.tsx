import ClipperForm from "../components/ClipperForm";

export default function Home() {
  return (
    <main>
      <h1>Clipper</h1>
      <p className="sub">
        Satu video YouTube menjadi beberapa klip vertikal bersubtitel, audio asli.
      </p>
      <ClipperForm />
    </main>
  );
}
