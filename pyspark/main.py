import optuna
import random
import nltk
from nltk.corpus import wordnet
from pyspark.sql import SparkSession
from transformers import DistilBertTokenizer, DistilBertForMaskedLM, Trainer, TrainingArguments, DataCollatorForLanguageModeling
from datasets import Dataset

# Download necessary NLTK resources for finding synonyms
nltk.download('wordnet')

def get_synonyms(word):
    """
    Fetches synonyms for a given word.

    Args:
    - word (str): The word for which to find synonyms.

    Returns:
    - list: A list of synonyms for the given word.
    """
    synonyms = set()
    for syn in wordnet.synsets(word):
        for lem in syn.lemmas():
            synonym = lem.name().replace('_', ' ')
            synonyms.add(synonym)
    if word in synonyms:
        synonyms.remove(word)
    return list(synonyms)

def synonym_replacement(sentence, n=1):
    """
    Replaces up to n words in the sentence with their synonyms.

    Args:
    - sentence (str): The sentence to augment.
    - n (int): The maximum number of words to replace.

    Returns:
    - str: The augmented sentence.
    """
    words = sentence.split()
    new_words = words.copy()
    random_word_list = list(set([word for word in words if wordnet.synsets(word)]))
    random.shuffle(random_word_list)
    num_replaced = 0
    for random_word in random_word_list:
        synonyms = get_synonyms(random_word)
        if len(synonyms) >= 1:
            synonym = random.choice(synonyms)
            new_words = [synonym if word == random_word else word for word in new_words]
            num_replaced += 1
        if num_replaced >= n: 
            break

    sentence = ' '.join(new_words)
    return sentence

def augment_example(example, augment_rate=0.1, n=1):
    """
    Augments the text by replacing n words with their synonyms with a given probability.

    Args:
    - example (dict): A dictionary containing the key 'text' with text to augment.
    - augment_rate (float): The probability of augmenting each sentence.
    - n (int): The number of words to replace with synonyms in each sentence.

    Returns:
    - dict: A dictionary containing the key 'text' with the augmented text.
    """
    augmented_text = synonym_replacement(example['text'], n=n) if random.uniform(0, 1) < augment_rate else example['text']
    return {"text": augmented_text}

# Initialize SparkSession
spark = SparkSession.builder \
    .appName("DistilBERT Training") \
    .getOrCreate()

def filter_sentences_by_token_limit(input_file_path: str, output_file_path: str, max_tokens: int = 512):
    """
    Filters sentences in a text file, excluding those that exceed a specified token limit.

    Args:
    - input_file_path (str): The path to the input text file.
    - output_file_path (str): The path where the filtered output text file will be saved.
    - max_tokens (int): The maximum number of tokens allowed per sentence.
    """
    tokenizer = DistilBertTokenizer.from_pretrained('distilbert-base-uncased')
    with open(input_file_path, 'r', encoding='utf-8') as input_file, open(output_file_path, 'w', encoding='utf-8') as output_file:
        for line in input_file:
            tokens = tokenizer.tokenize(line)
            if len(tokens) <= max_tokens:
                output_file.write(line)

input_file_path = "/Users/michalozieblo/Desktop/mapreducetorch/scraper/output/output.txt"
output_file_path = "/Users/michalozieblo/Desktop/mapreducetorch/scraper/output/filtered_output.txt"
filter_sentences_by_token_limit(input_file_path, output_file_path)

# Load and augment data
data = spark.read.text(output_file_path).rdd.map(lambda r: r[0]).collect()
data = Dataset.from_dict({'text': data})
augmented_data = data.map(lambda example: augment_example(example, augment_rate=0.3, n=2))

# Tokenize data
tokenizer = DistilBertTokenizer.from_pretrained("distilbert-base-uncased")
def tokenize_function(examples):
    """
    Tokenizes the texts from examples.

    Args:
    - examples (dict): A dictionary containing the key 'text' with a list of texts to tokenize.

    Returns:
    - dict: A dictionary containing tokenized texts.
    """
    return tokenizer(examples["text"], padding="max_length", truncation=True, max_length=512)

train_dataset = augmented_data.map(tokenize_function, batched=True)

def model_training_function(trial):
    """
    Objective function for Optuna optimization. Trains the model and returns the evaluation loss.

    Args:
    - trial (optuna.trial.Trial): An Optuna trial object.

    Returns:
    - float: The evaluation loss of the model.
    """
    model = DistilBertForMaskedLM.from_pretrained("distilbert-base-uncased")
    training_args = TrainingArguments(
        output_dir=f'./results_trial_{trial.number}',
        num_train_epochs=trial.suggest_int("num_train_epochs", 1, 5),
        per_device_train_batch_size=trial.suggest_categorical("per_device_train_batch_size", [8, 16]),
        learning_rate=trial.suggest_float("learning_rate", 5e-5, 5e-4),
        logging_dir='./logs',
        logging_steps=10,
        use_cpu=True
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,  
        data_collator=DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=True, mlm_probability=0.15),
    )

    trainer.train()
    eval_results = trainer.evaluate()

    model.save_pretrained(f'./best_model_trial_{trial.number}')

    return eval_results["eval_loss"]

# Run Optuna optimization
study = optuna.create_study(direction="minimize")
study.optimize(model_training_function, n_trials=3)

best_trial = study.best_trial
print(f"Best trial: {best_trial.number} with loss {best_trial.value}")

# Load the best model
best_model_path = f'./best_model_trial_{best_trial.number}'
model = DistilBertForMaskedLM.from_pretrained(best_model_path)
