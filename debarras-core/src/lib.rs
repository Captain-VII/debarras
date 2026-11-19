//! Cœur de Débarras, porté depuis le paquet Python `core/` (voir le plan de migration).
//! Aucune dépendance à l'interface : la couche Tauri appelle ces modules.

pub mod actions;
pub mod cache;
pub mod cleanup;
pub mod drives;
pub mod duplicates;
pub mod filetypes;
pub mod finder;
pub mod history;
pub mod paths;
pub mod recycle;
pub mod safety;
pub mod samename;
pub mod scanner;
pub mod search;
pub mod similar;
pub mod stats;
#[cfg(test)]
mod testutil;
