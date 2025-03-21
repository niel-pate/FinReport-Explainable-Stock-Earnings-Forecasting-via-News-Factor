import os
import sys
import yaml
import torch
import numpy as np
import pandas as pd
import logging
from torch.utils.data import Dataset, DataLoader
from model import FinReportModel
from data_loader import load_data, split_data
from preprocessing import select_features, normalize_features, rename_technical_columns
from report_generator import generate_html_finreport, save_html_report
from sentiment import get_sentiment_score
from extra_factors import (compute_market_factor, compute_size_factor, compute_valuation_factor, 
                           compute_profitability_factor, compute_investment_factor, compute_news_effect_factor,
                           compute_rsi_factor, compute_mfi_factor, compute_bias_factor)
from risk_model import compute_max_drawdown, compute_volatility
from advanced_news import compute_event_factor
from sklearn.metrics import (accuracy_score, roc_auc_score, f1_score, average_precision_score,
                             precision_score, recall_score, confusion_matrix)
from jinja2 import Environment, FileSystemLoader
from news_aggregator import aggregate_news_factors
import matplotlib.pyplot as plt        # NEW CODE: Import for plotting
import seaborn as sns                  # NEW CODE: Import for plotting
import sys
sys.path.append('.')  # Make sure current directory is in path
from improved_metrics import (evaluate_binary_classification, 
                              aggregate_stock_metrics, 
                              create_metrics_heatmap)

# ----- Set Up Logging to File and Terminal -----
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[
        logging.FileHandler("evalute_output.txt", mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger()

# ----- Additional Functions -----
def compute_integrated_risk(predicted_return, volatility):
    return predicted_return / volatility if volatility != 0 else np.nan

def compute_cvar(returns, alpha=0.05):
    sorted_returns = np.sort(returns)
    index = int(alpha * len(sorted_returns))
    return np.mean(sorted_returns[:index])

# Add new function to optimize threshold based on F1-score.
def optimize_threshold(true_labels, predicted_values):
    # Handle case with very few samples
    if len(predicted_values) <= 5:
        default_threshold = np.mean(predicted_values) * 0.9  # Slightly lower than mean
        logger.info(f"Few samples ({len(predicted_values)}), using default threshold: {default_threshold:.3f}")
        return default_threshold, 0
    # Original code for larger datasets
    thresholds = np.linspace(np.min(predicted_values), np.max(predicted_values), 100)
    best_threshold = thresholds[0]
    best_f1 = 0
    for thresh in thresholds:
        binary_preds = (predicted_values > thresh).astype(int)
        current_f1 = f1_score(true_labels, binary_preds, zero_division=0)
        if current_f1 > best_f1:
            best_f1 = current_f1
            best_threshold = thresh
    return best_threshold, best_f1

# NEW FUNCTION: Analyze threshold placement
def analyze_threshold_placement(predicted_values, true_labels, threshold):
    # Convert to numpy arrays if needed
    pred_values = np.array(predicted_values)
    true_vals = np.array(true_labels)
    
    # Statistics of separation
    pred_pos = pred_values[true_vals > 0]
    pred_neg = pred_values[true_vals <= 0]
    
    # If no positive or negative samples, return basic info
    if len(pred_pos) == 0 or len(pred_neg) == 0:
        return {
            "pos_samples": len(pred_pos),
            "neg_samples": len(pred_neg),
            "threshold": threshold,
            "separation": "N/A - only one class present"
        }
    
    # Calculate mean and std of each group
    pos_mean = np.mean(pred_pos)
    neg_mean = np.mean(pred_neg)
    pos_std = np.std(pred_pos)
    neg_std = np.std(pred_neg)
    
    # Calculate separation metrics
    separation = (pos_mean - neg_mean) / (pos_std + neg_std) if (pos_std + neg_std) > 0 else float('inf')
    
    # Check if threshold is well-placed
    threshold_to_pos = (threshold - pos_mean) / pos_std if pos_std > 0 else float('inf')
    threshold_to_neg = (neg_mean - threshold) / neg_std if neg_std > 0 else float('inf')
    
    well_placed = min(threshold_to_pos, threshold_to_neg) > 0
    
    return {
        "pos_samples": len(pred_pos),
        "neg_samples": len(pred_neg),
        "pos_mean": pos_mean,
        "neg_mean": neg_mean,
        "separation": separation,
        "threshold": threshold,
        "well_placed": well_placed,
        "threshold_pos_distance": threshold_to_pos,
        "threshold_neg_distance": threshold_to_neg
    }

# New function to visualize prediction distributions
def visualize_prediction_distribution(stock, predictions, true_labels, threshold):
    plt.figure(figsize=(12, 6))
    
    # Plot 1: Predictions vs True Labels
    plt.subplot(1, 2, 1)
    plt.scatter(true_labels, predictions, alpha=0.7)
    plt.plot([min(true_labels), max(true_labels)], [min(true_labels), max(true_labels)], 'r--')
    plt.xlabel("True Labels")
    plt.ylabel("Predictions")
    plt.title(f"Predictions vs True Labels for {stock}")
    plt.grid(True, alpha=0.3)
    
    # Plot 2: Distribution of Predictions and True Labels
    plt.subplot(1, 2, 2)
    plt.hist(true_labels, bins=10, alpha=0.5, label="True Labels")
    plt.hist(predictions, bins=10, alpha=0.5, label="Predictions")
    plt.axvline(x=threshold, color='r', linestyle='--', label=f'Threshold: {threshold:.3f}')
    plt.xlabel("Value")
    plt.ylabel("Frequency")
    plt.title(f"Distribution of Values for {stock}")
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    plt.tight_layout()
    image_path = os.path.join('img', f'distribution_{stock}.png')
    plt.savefig(image_path)
    plt.close()
    
    logger.info(f"Prediction distribution visualization saved to {image_path}")

# ----- Load Configuration -----
with open('src/config.yaml', 'r') as f:
    config = yaml.safe_load(f)

data_path   = config['data_path']
batch_size  = config['batch_size']
seq_len     = config['seq_len']
model_config = config['model']
input_size  = model_config['input_size']
hidden_size = model_config['hidden_size']
num_layers  = model_config.get('num_layers', 1)
dropout     = model_config.get('dropout', 0.0)

# ----- Load Data and Rename Columns -----
df = load_data(data_path)
df = rename_technical_columns(df)
logger.info("Columns after renaming:")
logger.info(list(df.columns))

stock_list = df['ts_code'].unique()

# ----- Load Model -----
model = FinReportModel(input_size=input_size, hidden_size=hidden_size, num_layers=num_layers)
model.load_state_dict(torch.load('models/finreport_model.pth'))
model.eval()

# ----- Define Dataset -----
class FinDataset(Dataset):
    def __init__(self, features, labels, seq_len):
        # Truncate features and labels to the same length
        min_len = min(len(features), len(labels))
        self.features = features[:min_len]
        self.labels = labels[:min_len]
        self.seq_len = seq_len
    def __len__(self):
        return max(0, len(self.features) - self.seq_len)
    def __getitem__(self, idx):
        x = self.features[idx: idx + self.seq_len]
        y = self.labels[idx + self.seq_len - 1]
        return torch.tensor(x, dtype=torch.float), torch.tensor(y, dtype=torch.float)

# ----- Initialize Lists for Metrics and Reports -----
all_metrics = []
all_reports = []
all_detailed_metrics = []  # New list to store detailed metrics

os.makedirs('img', exist_ok=True)

# ----- Process Each Stock -----
for stock in stock_list:
    logger.info(f"Processing stock: {stock}")
    df_stock = df[df['ts_code'] == stock]
    
    row_count = len(df_stock)
    logger.info(f"Stock {stock} has {row_count} rows.")
    if row_count <= seq_len:
        logger.info(f"Not enough data for stock {stock} (requires > {seq_len} rows). Skipping.")
        continue

    latest_val = df_stock['market_value'].iloc[-1]
    avg_val = df_stock['market_value'].mean()
    diff_percent = ((latest_val - avg_val) / avg_val) * 100
    logger.info(f"For stock {stock}: Latest market value = {latest_val}, Average = {avg_val}, Difference = {diff_percent:.1f}%")
    
    _, test_df = split_data(df_stock)
    # Prepare training data for calibration
    train_df, _ = split_data(df_stock, train_ratio=0.6)  # Use same ratio as before
    train_features, train_labels = select_features(train_df)
    train_features, _ = normalize_features(train_features)
    train_dataset = FinDataset(train_features, train_labels, seq_len)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=False)

    # Get training predictions for calibration
    train_predictions = []
    train_true_labels = []
    with torch.no_grad():
        for x_batch, y_batch in train_loader:
            preds = model(x_batch)
            train_predictions.extend(preds.cpu().numpy().flatten())
            train_true_labels.extend(y_batch.numpy())
    
    if len(test_df) <= seq_len:
        logger.info(f"Not enough test data for stock {stock} (requires > {seq_len} rows). Skipping.")
        continue

    test_features, test_labels = select_features(test_df)
    unique, counts = np.unique(test_labels, return_counts=True)
    logger.info("True label distribution: " + str(dict(zip(unique, counts))))
    mean_val = np.mean(test_labels)
    median_val = np.median(test_labels)
    variance_val = np.var(test_labels)
    logger.info(f"Target Return Distribution - Mean: {mean_val:.3f}, Median: {median_val:.3f}, Variance: {variance_val:.3f}")
    logger.info("Shape of features: " + str(test_features.shape))
    logger.info("First row of features: " + str(test_features[0]))
    test_features, scaler = normalize_features(test_features)
    dataset = FinDataset(test_features, test_labels, seq_len)
    if len(dataset) <= 0:
        logger.info(f"Dataset for stock {stock} is empty after processing. Skipping.")
        continue

    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False)
    all_predictions = []
    with torch.no_grad():
        for x_batch, _ in loader:
            preds = model(x_batch)
            all_predictions.extend(preds.cpu().numpy().flatten())
    all_predictions = np.array(all_predictions)

    # NEW CODE: Log raw prediction statistics.
    unique_preds, counts_preds = np.unique(all_predictions, return_counts=True)
    logger.info(f"Raw predicted values distribution: {dict(zip(unique_preds, counts_preds))}")
    logger.info(f"Mean predicted value: {np.mean(all_predictions):.3f}, Median: {np.median(all_predictions):.3f}")

    predicted_return = all_predictions[0]

    # --- Replace Classification Metrics Section ---
    # Get the true labels corresponding to predictions
    true_labels = test_labels[seq_len-1:seq_len-1+len(all_predictions)]

    # Get improved classification metrics
    classification_metrics = evaluate_binary_classification(
        predictions=all_predictions,
        true_labels=true_labels,
        stock_name=stock,
        train_preds=np.array(train_predictions),
        train_labels=np.array(train_true_labels),
        calibrate=True,
        plot=True
    )

    # Compute consistent binary true labels for metrics
    binary_true = (true_labels > 0).astype(int)
    accuracy = accuracy_score(binary_true, classification_metrics['binary_preds'])
    precision = precision_score(binary_true, classification_metrics['binary_preds'], zero_division=0)
    recall = recall_score(binary_true, classification_metrics['binary_preds'], zero_division=0)
    f1 = f1_score(binary_true, classification_metrics['binary_preds'], zero_division=0)
    auc = classification_metrics['auc']
    aupr = classification_metrics['aupr']
    error_rate = 1 - accuracy
    specificity = classification_metrics.get('specificity', 0)
    
    print("----------------------------------------------------------")
    print(f"{'Metric':<20} | {'Value':>8}")
    print("----------------------------------------------------------")
    print(f"{'Accuracy':<20} | {accuracy:>8.3f}")
    print(f"{'Precision':<20} | {precision:>8.3f}")
    print(f"{'Recall (Sensitivity)':<20} | {recall:>8.3f}")
    print(f"{'Specificity':<20} | {specificity:>8.3f}")
    print(f"{'F1-score':<20} | {f1:>8.3f}")
    print(f"{'AUC':<20} | {auc:>8.3f}")
    print(f"{'AUPR':<20} | {aupr:>8.3f}")
    print(f"{'Error Rate':<20} | {error_rate:>8.3f}")
    print("----------------------------------------------------------")
    
    # NEW CALL: Visualize prediction distribution
    visualize_prediction_distribution(
        stock, 
        all_predictions, 
        test_labels[seq_len-1:seq_len-1+len(all_predictions)],
        classification_metrics['threshold']
    )
    
    logger.info(f"First 10 Raw Predictions for {stock}: {all_predictions[:10]}")
    logger.info(f"First 10 Ground Truth Labels for {stock}: {true_labels[:10]}")
    
    # NEW CODE: Log binary predictions distribution.
    unique_preds, counts_preds = np.unique(classification_metrics['binary_preds'], return_counts=True)
    
    # NEW CALL: Analyze threshold placement and log the results.
    threshold_analysis = analyze_threshold_placement(all_predictions, true_labels, classification_metrics['threshold'])
    logger.info(f"Threshold analysis for {stock}:")
    logger.info(f"  Positive samples: {threshold_analysis['pos_samples']}")
    logger.info(f"  Negative samples: {threshold_analysis['neg_samples']}")
    logger.info(f"  Separation metric: {threshold_analysis.get('separation', 'N/A')}")
    logger.info(f"  Threshold well placed: {threshold_analysis.get('well_placed', 'N/A')}")
    
    logger.info(f"Binary Predictions Distribution for {stock}: {dict(zip(unique_preds, counts_preds))}")

    # NEW CHECK: Log class distribution and handle imbalance.
    class_counts = np.bincount(true_labels.astype(int))
    logger.info(f"Class distribution for {stock}: {class_counts}")
    if len(class_counts) < 2 or min(class_counts) < 2:
        logger.warning(f"Severe class imbalance for {stock}. Using stratified metrics.")
        if len(np.unique(true_labels)) < 2:
            auc = 0.5
            aupr = 0.5
            logger.warning(f"Only one class present for {stock}. Setting AUC/AUPR to 0.5.")
        else:
            try:
                auc = roc_auc_score(true_labels, all_predictions)
                aupr = average_precision_score(true_labels, all_predictions)
            except Exception as e:
                logger.warning(f"Error calculating metrics for {stock}: {e}")
                auc = 0.5
                aupr = 0.5
    else:
        # NEW CHANGE: Replace multiclass metric calculation with binary conversion for consistency.
        binary_true = (true_labels > 0).astype(int)
        try:
            auc = roc_auc_score(binary_true, all_predictions)
            aupr = average_precision_score(binary_true, all_predictions)
        except Exception as e:
            logger.warning(f"Error calculating metrics for {stock}: {e}")
            auc = 0.5
            aupr = 0.5
    # Compute confusion matrix if possible.
    if len(np.unique(true_labels)) < 2:
        tn, fp, fn, tp = 0, 0, 0, 0
    else:
        binary_true = (true_labels > 0).astype(int)   # NEW: Convert continuous true labels to binary
        cm = confusion_matrix(binary_true, classification_metrics['binary_preds'], labels=[0, 1])
        logger.info(f"Confusion Matrix for stock {stock}:\n{cm}")
        if cm.shape == (2, 2):
            tn, fp, fn, tp = cm.ravel()
            # Plot and save confusion matrix as an image
            plt.figure(figsize=(4, 3))
            sns.heatmap(cm, annot=True, fmt="d", cmap="Blues", xticklabels=[0, 1], yticklabels=[0, 1])
            plt.xlabel("Predicted")
            plt.ylabel("True")
            plt.title(f"Confusion Matrix for stock {stock}")
            image_filename = os.path.join('img', f'confusion_matrix_{stock}.png')
            plt.savefig(image_filename)
            plt.close()
            logger.info(f"Confusion matrix image saved as {image_filename}")
        else:
            logger.warning(f"Confusion Matrix for stock {stock} is not 2x2 due to class imbalance. Using fallback values.")
            tn, fp, fn, tp = 0, 0, 0, 0

    # Compute regression metrics
    from sklearn.metrics import mean_squared_error, mean_absolute_error
    mse_value = mean_squared_error(test_labels[seq_len-1:seq_len-1+len(all_predictions)], all_predictions)
    rmse_value = np.sqrt(mse_value)
    mae_value = mean_absolute_error(test_labels[seq_len-1:seq_len-1+len(all_predictions)], all_predictions)

    # Store metrics for the current stock
    all_metrics.append({
        'Stock': stock,
        'Accuracy': accuracy,
        'Precision': precision,
        'Recall': recall,
        'Specificity': specificity,
        'F1-score': f1,
        'AUC': auc,
        'AUPR': aupr,
        'Error Rate': error_rate,
        'MSE': mse_value,   # regression metrics stored here
        'RMSE': rmse_value,
        'MAE': mae_value
    })
    
    # Also store detailed metrics for enhanced reporting
    all_detailed_metrics.append({
        'Stock': stock,
        'Samples': classification_metrics['samples'],
        'Class_Balance': classification_metrics['class_balance'],
        'Confidence': classification_metrics.get('confidence', 0),
        'Threshold': classification_metrics['threshold'],
        'Accuracy': accuracy,
        'Precision': precision,
        'Recall': recall,
        'F1-score': f1,
        'AUC': auc,
        'AUPR': aupr,
        'MSE': mse_value,
        'RMSE': rmse_value,
        'MAE': mae_value
    })

    news_summary = str(test_df.iloc[-1]["announcement"])
    sentiment_score = get_sentiment_score(news_summary)
    logger.info(f"Sentiment Score for {stock}: {sentiment_score:.4f}")
    news_source = "财联社"

    market_factor = compute_market_factor(df_stock)
    size_factor = compute_size_factor(df_stock)
    valuation_factor = compute_valuation_factor(df_stock)
    profitability_factor = compute_profitability_factor(df_stock)
    investment_factor = compute_investment_factor(df_stock)
    news_effect_factor = compute_news_effect_factor(sentiment_score)
    event_factor = compute_event_factor(news_summary)
    rsi_factor = compute_rsi_factor(df_stock)
    mfi_factor = compute_mfi_factor(df_stock)
    bias_factor = compute_bias_factor(df_stock)

    returns = df_stock['close'].pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    try:
        vol, var_95 = compute_volatility(returns)
        max_dd = compute_max_drawdown(returns)
        cvar = compute_cvar(returns, alpha=0.05)
        risk_adjusted_ratio = compute_integrated_risk(predicted_return, vol)
        risk_metrics = {
            "volatility": f"{vol:.2f}",
            "var_95": f"{var_95:.2f}",
            "max_drawdown": f"{max_dd:.2f}",
            "cvar": f"{cvar:.2f}",
            "risk_adjusted_ratio": f"{risk_adjusted_ratio:.2f}"
        }
    except ValueError as e:
        risk_metrics = {"error": str(e)}

    risk_assessment = "Low/Moderate Risk" if predicted_return < 2.0 else "High Risk"
    overall_trend = "Positive"
    final_summary = ("Based on current technical indicators and recent news, the outlook for this stock appears positive. "
                     "However, market conditions remain volatile, so caution is advised.")
    date_str = "Today"

    report_html = generate_html_finreport(
        stock_symbol=stock,
        date_str=date_str,
        news_summary=news_summary,
        news_source=news_source,
        market_factor=market_factor,
        size_factor=size_factor,
        valuation_factor=valuation_factor,
        profitability_factor=profitability_factor,
        investment_factor=investment_factor,
        news_effect_factor=news_effect_factor,
        event_factor=event_factor,
        rsi_factor=rsi_factor,
        mfi_factor=mfi_factor,
        bias_factor=bias_factor,
        risk_assessment=risk_assessment,
        overall_trend=overall_trend,
        news_effect_score=news_effect_factor["value"],
        risk_metrics=risk_metrics,
        template_path="templates/report_template.html"
    )
    all_reports.append(report_html)
    logger.info(f"Report for {stock} generated.")
    target_mean = np.mean(all_predictions)
    target_median = np.median(all_predictions)
    target_variance = np.var(all_predictions)
    logger.info(f"Target Return Distribution - Mean: {target_mean:.3f}, Median: {target_median:.3f}, Variance: {target_variance:.3f}")

# ----- After Loop: Print Combined Metrics Table -----
if all_metrics:
    df_metrics = pd.DataFrame(all_metrics)
    logger.info("\nOverall Performance Metrics for All Stocks:")
    logger.info(df_metrics.to_string(index=False))
    
    # Create enhanced metrics DataFrame
    df_detailed = pd.DataFrame(all_detailed_metrics)
    
    # Add reliability flag
    df_detailed['Reliable'] = (df_detailed['Samples'] >= 20) & (df_detailed['Class_Balance'] >= 0.2)
    
    # Enhanced heatmap for classification metrics
    create_metrics_heatmap(
        df_detailed, 
        ['AUC', 'AUPR', 'F1-score', 'Accuracy'],
        "Classification Metrics with Reliability Indicators", 
        os.path.join('img', "enhanced_classification_metrics.png")
    )
    
    # Enhanced heatmap for regression metrics
    create_metrics_heatmap(
        df_detailed,
        ['MSE', 'RMSE', 'MAE'],
        "Regression Metrics by Stock",
        os.path.join('img', "enhanced_regression_metrics.png")
    )
    
    # Create a summary report with weighted averages
    _, weighted_metrics = aggregate_stock_metrics(
        df_detailed.to_dict('records'), 
        df_detailed['Stock'].tolist()
    )
    
    # Print weighted averages (more reliable than simple means)
    logger.info("\nWeighted Metric Averages (adjusted for sample size and class balance):")
    for metric, value in weighted_metrics.items():
        logger.info(f"{metric}: {value:.4f}")
    
    # Save detailed metrics to CSV
    df_detailed.to_csv('enhanced_evaluation_results.csv', index=False)
    logger.info("Enhanced evaluation results saved to enhanced_evaluation_results.csv")
else:
    logger.info("No performance metrics were computed.")

if all_reports:
    from report_generator import generate_multi_report_html
    final_html = generate_multi_report_html(all_reports, template_path="templates/multi_report_template.html")
    save_html_report(final_html, output_filename="finreport_combined.html")
else:
    logger.info("No reports were generated due to insufficient data across stocks.")
logger.info("Evaluation completed.")