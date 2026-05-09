# toolbox

Holding pen for assets and bundles.

## holyland.tar.gz

Source tarball of the **Holy Land** game project (Session 1 / Half 1: desktop hello-world). Lives here because the sandbox that produced it is allowlisted only to the campjun-org repos `wyrdlands`, `hexmaster`, and `toolbox` — direct push to a separate `holy-land` repo isn't possible from inside the sandbox.

To extract and use:

```bash
mkdir holy-land && cd holy-land
tar -xzf path/to/holyland.tar.gz
cargo run --release   # first build is slow; bundles SDL2 from source
```

Then `git init` / `git remote add` / `git push` from your own machine to `campjun/holy-land`.

Contains: full source (`src/`), `Cargo.toml`, `.cargo/config.toml`, `.gitignore`, `assets/cp437_16x16.png` (Guybrush CP437 tileset), and project README. No `target/`, no `Cargo.lock`. ~17KB.

Plan + design rationale lives in the chat session that produced it.
