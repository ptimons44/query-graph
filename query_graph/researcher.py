# Researcher.py
import re
import requests
from bs4 import BeautifulSoup
import json


import spacy 
from scipy.spatial.distance import cosine

from gpt import callGPT
import config
from logger import logger

import time

from sentence_transformers import SentenceTransformer, util



    
class Parser:
    def __init__(self, researcher, nlp):
        self.researcher = researcher

        self.search_queries = self.generate_search_queries(
            self.researcher.query, 
            self.researcher.gpt_response, 
            nlp,
            self.researcher.threshold
        )

    def levenshtein_distance(self, s1, s2):
        if len(s1) < len(s2):
            return self.levenshtein_distance(s2, s1)
        if len(s2) == 0:
            return len(s1)
        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        return previous_row[-1]

    def get_sentence_index(self, phrase, sentences):
        """_summary_

        Args:
            phrase (spacy.span): phrase we want the sentence index of
            sentences (list): list of sentences (spans)

        Returns:
            int: index of phrase in sentences
        """
        for ix, sentence in enumerate(sentences):
            if sentence.end >= phrase.end:
                index = ix
                break
        return index

    # TODO: This may be a good thing to replace with LangChain
    def generate_search_queries(self, query, gpt_response, nlp, threshold):
        """_summary_

        Args:
            input (string): query from which we want to extract keywords
            nlp (space.Language): a model (either off-the-shelf or custom) that creates Doc object

        Returns:
            list: list of strings, where each string is a noun phrase
        """
        gpt_doc = nlp(gpt_response)
        query_doc = nlp(query)
        gpt_keywords = list(gpt_doc.noun_chunks)
        query_keywords = list(query_doc.noun_chunks)
        gpt_sentences = list(gpt_doc.sents)

        search_queries = set()
        for gpt_word in gpt_keywords:
            max_similarity = 1
            most_similar = None
            for query_word in query_keywords:
                is_duplicate = False

                similarity = cosine(gpt_word.vector, query_word.vector)
                if similarity < max_similarity:
                    max_similarity = similarity
                    most_similar = query_word
            
            if max_similarity <= threshold:
                position = self.get_sentence_index(gpt_word, gpt_sentences)

                # disregard search_query if we have already generated a similar search_query
                for generated_query in search_queries:
                    distance = self.levenshtein_distance(
                        generated_query.query_keyword + " " + generated_query.gpt_keyword,
                        query_word.text + " " + gpt_word.text
                    )
                    if distance < self.researcher.similarity_threshold:
                        generated_query.gpt_sentences.append(position)
                        is_duplicate = True
                
                if is_duplicate:
                    continue

                OR_search_query = SearchQuery(
                    most_similar.text + " OR " + gpt_word.text, 
                    most_similar.text,
                    gpt_word.text,
                    [position]
                )
                search_queries.add(OR_search_query)

                # disregard AND query if the phrases are near duplicates / spelling variations
                if self.levenshtein_distance(most_similar.text, gpt_word.text) < 3:
                    continue

                AND_search_query = SearchQuery(
                    most_similar.text + " AND " + gpt_word.text, 
                    most_similar.text,
                    gpt_word.text,
                    [position]
                )
                search_queries.add(AND_search_query)
                
            
        return search_queries


class SearchQuery:
    def __init__(self, text, query_keyword, gpt_keyword, gpt_sentences):
        self.text = text
        self.query_keyword = query_keyword
        self.gpt_keyword = gpt_keyword
        self.gpt_sentences = gpt_sentences

class Search:
    def __init__(self, search_query):
        self.search_query = search_query  

    def search_google(self, results_per_search):
        """
        before is string in YYYY-MM-DD format
        """
        output = []
        while results_per_search > 0:
            if results_per_search < 10:
                page=1
                num=results_per_search
            else:
                num=10
                page=1
            params = {
                "key": config.GGLSEARCH_APIKEY(),
                "cx": config.GGL_SE(),
                "q": self.search_query.text,
                "h1": "en",
                "lr": "lang_en",
                "page": page,
                "num": num,
                # "condition": AUTO # need to fix this
            }

            response = requests.get(config.GGLSEARCH_URL(), params=params)
            assert (int(response.status_code) > 199 and int(response.status_code) < 300), "Google API Non-Responsive. Check search quotas. Error: " + str(response.status_code)
            response = json.loads(response.content)
            response["error"] = 0
            for item in response["items"]:
                output.append(item)
            results_per_search -= num
            page += 1
        return list(item["link"] for item in output)




class Researcher(object):
    def __init__(self, query, **kwargs):
        self.query = query
        self.threshold = kwargs.get("threshold", 0.4)
        self.similarity_threshold = kwargs.get("similarity_threshold", 12)
        self.results_per_search = kwargs.get("results_per_search", 5)
        self.num_nodes = kwargs.get("num_nodes", 100)
        self.context_window = kwargs.get("context_window", 2)
        self.search_resolution = kwargs.get("search_resolution", 10000) 

        nlp = kwargs.get("nlp", spacy.load("en_core_web_sm"))

        self.gpt_response = self.ask_gpt_query(query)
        self.gpt_sentences = Page.split_into_sentences(self, self.gpt_response) 

        self.parser = Parser(self, nlp)
        self.search_queries = self.parser.search_queries
        logger.info(f"Trying the following search queries: {[q.text for q in self.search_queries]}")


    def ask_gpt_query(self, query):
        with open("query_graph/gpt_prompts.json", "r") as f:
            prompt = json.loads(f.read())["initial prompt"]
        prompt += query
        response = callGPT(prompt)
        return response
    
    def get_urls(self, search_query, url_dict):
        """_summary_

        Args:
            url_dict (shared dictionary): maps url (string) to search queries (set of strings)
            search_query (_type_): _description_
        """
        search = Search(search_query)
        for url in search.search_google(self.results_per_search):
            if url not in url_dict:
                url_dict[url] = {search_query}
            else:
                url_dict[url].add(search_query)

    def create_page(self, search_queries, url, pages_dict):
        page = Page(search_queries, url)
        pages_dict[url] = page

    def create_pages_and_sentences(self, search_queries, url, sentence_list):
        logger.debug(f"creating page and sentences for {url}")
        page = Page(search_queries, url)
        if page.content:
            for (position, sentence_text) in enumerate(page.sentences):
                sentence_list.append(
                    Sentence(
                        search_queries,
                        sentence_text,
                        page.get_sentence_content(position, self.context_window), # context
                        len(sentence_list) # index
                    )
                )
                # sentence.embedding = model.encode(sentence.sentence)
                # sentence.relevance = sentence.embedding.dot(self.gpt_response_embedding)
                # sentence_list.append(sentence)

    # def create_sentence(self, search_queries, sentence_text, context, model, sentence_list):
    #     # print("creating sentence: ", sentence_text)
    #     sentence = Sentence(
    #         search_queries,
    #         sentence_text,
    #         context
    #     )
    #     sentence_list.append(sentence)

    

class Page():
    def __init__(self, search_queries, url): #content, url, ranking):
        self.search_queries = search_queries # the search query(ies) that returned this page
        self.url = url

        self.content = self.get_webpage_content()
        if self.content:
            self.sentences = self.split_into_sentences(self.content)
            logger.debug(f"page initialized with {len(self.sentences)} sentences from {self.url}")
            if len(self.sentences) > 500:
                logger.debug(f"{url} has more than 500 sentences")
                # logger.debug(f"{url} has more that 500 sentences. Sentences after 500: {self.sentences[500:]}")
        else:
            self.sentneces = []
        

    def get_webpage_content(self):
        logger.debug(f"getting content from {self.url}")
        try:
            # Send a GET request to the specified URL
            response = requests.get(self.url)
            logger.debug(f"retrieving content from {self.url}")

            # Check if the request was successful (status code 200)
            if response.status_code == 200:
                # Parse the HTML content of the webpage
                soup = BeautifulSoup(response.content, 'html.parser')

                # Extract the textual content from the parsed HTML
                # For example, if you want to get the text from all paragraphs:
                paragraphs = soup.find_all('p')
                content = ' '.join([p.get_text() for p in paragraphs])
                
                logger.debug(f"returning content from {self.url}")
                return content
            else:
                logger.debug(f"Request failed with status code: {response.status_code}")
        except requests.RequestException as e:
            logger.debug(f"An error occurred: {e}")
        
        logger.debug(f"unable to retrieve content from {self.url}")
        return None


    def split_into_sentences(self, text: str) -> list[str]:
        """
        Split the text into sentences.

        If the text contains substrings "<prd>" or "<stop>", they would lead 
        to incorrect splitting because they are used as markers for splitting.

        :param text: text to be split into sentences
        :type text: str

        :return: list of sentences
        :rtype: list[str]
        """
        # -*- coding: utf-8 -*-
        alphabets= "([A-Za-z])"
        prefixes = "(Mr|St|Mrs|Ms|Dr)[.]"
        suffixes = "(Inc|Ltd|Jr|Sr|Co)"
        starters = "(Mr|Mrs|Ms|Dr|Prof|Capt|Cpt|Lt|He\s|She\s|It\s|They\s|Their\s|Our\s|We\s|But\s|However\s|That\s|This\s|Wherever)"
        acronyms = "([A-Z][.][A-Z][.](?:[A-Z][.])?)"
        websites = "[.](com|net|org|io|gov|edu|me)"
        digits = "([0-9])"
        multiple_dots = r'\.{2,}'
        text = " " + text + "  "
        text = text.replace("\n"," ")
        text = re.sub(prefixes,"\\1<prd>",text)
        text = re.sub(websites,"<prd>\\1",text)
        text = re.sub(digits + "[.]" + digits,"\\1<prd>\\2",text)
        text = re.sub(multiple_dots, lambda match: "<prd>" * len(match.group(0)) + "<stop>", text)
        if "Ph.D" in text: text = text.replace("Ph.D.","Ph<prd>D<prd>")
        text = re.sub("\s" + alphabets + "[.] "," \\1<prd> ",text)
        text = re.sub(acronyms+" "+starters,"\\1<stop> \\2",text)
        text = re.sub(alphabets + "[.]" + alphabets + "[.]" + alphabets + "[.]","\\1<prd>\\2<prd>\\3<prd>",text)
        text = re.sub(alphabets + "[.]" + alphabets + "[.]","\\1<prd>\\2<prd>",text)
        text = re.sub(" "+suffixes+"[.] "+starters," \\1<stop> \\2",text)
        text = re.sub(" "+suffixes+"[.]"," \\1<prd>",text)
        text = re.sub(" " + alphabets + "[.]"," \\1<prd>",text)
        if "”" in text: text = text.replace(".”","”.")
        if "\"" in text: text = text.replace(".\"","\".")
        if "!" in text: text = text.replace("!\"","\"!")
        if "?" in text: text = text.replace("?\"","\"?")
        text = text.replace(".",".<stop>")
        text = text.replace("?","?<stop>")
        text = text.replace("!","!<stop>")
        text = text.replace("<prd>",".")
        sentences = text.split("<stop>")
        sentences = [s.strip() for s in sentences]
        if sentences and not sentences[-1]: sentences = sentences[:-1]
        return sentences
    
    def get_sentence_content(self, position, context_window):
        text = self.sentences[position]
        pre_context, post_context = "", ""
        if position > 0 and context_window > 0:
                pre_context = " ".join(self.sentences[max(0, position-context_window):position]).strip()
        if position < len(self.sentences) - 1 and context_window > 0:
            post_context = " ".join(self.sentences[position+1:min(len(self.sentences)-1, position+context_window+1)]).strip()
        return pre_context + " " + text + " " + post_context
    
class Sentence(Page):
    def __init__(self, search_queries, sentence, context, index):
        self.search_queries = search_queries
        self.text = sentence
        self.context = context
        self.index = index

        # self.similarities = {} # populated in get_top_k_similar_sentences
        # self.relation_to_gpt = {} # populated in get_relation_to_gpt

if __name__ == "__main__":
    # page = Page({"Marajuana"}, "https://en.wikipedia.org/wiki/Taiwan")
    # for sentence in page.sentences:
    #     print(sentence)
    #     print()
    gpt_response = """ChatGPT: To determine the deadliest animals in Australia, I would consider various factors such as the number of human fatalities caused by different species, the toxicity or venomous nature of the animals, and the likelihood or frequency of encounters with these dangerous creatures. It is important to note that a species being deadly does not necessarily mean it is aggressive or inclined to attack humans, but rather that it poses a potential threat due to its natural characteristics.

One of the most feared and deadliest animals in Australia is the saltwater crocodile (Crocodylus porosus). These massive reptiles are known to be highly aggressive and can be found in coastal areas, rivers, and even some open sea areas in the northern parts of Australia. Saltwater crocodiles are responsible for the highest number of reported fatal attacks on humans in the country. They are particularly dangerous as they are excellent swimmers and ambush predators, capable of striking suddenly with their powerful jaws.

Another dangerous animal in Australia is the box jellyfish (Chironex fleckeri). This marine creature, found in the coastal waters of Northern Australia, possesses extremely potent venom in its tentacles. Box jellyfish stings can cause cardiac arrest and death within minutes, making them one of the deadliest creatures in the ocean. While encounters with box jellyfish are rare and there are protective measures in place at popular swimming locations, their presence highlights the need for caution during marine activities.

Australia is also home to a variety of venomous snakes, including the inland taipan (Oxyuranus microlepidotus) and the eastern brown snake (Pseudonaja textilis). The inland taipan is considered the most venomous snake in the world, with its venom being highly potent and capable of causing rapid paralysis and death. Eastern brown snakes, on the other hand, are responsible for the highest number of snakebite-related deaths in Australia. These snakes are commonly found in populated areas, and their bites can lead to cardiovascular collapse and nervous system failure if not treated promptly.

In addition to the above, other notable deadly animals in Australia include the Sydney funnel-web spider (Atrax robustus), known for its highly toxic venom, and the cone snail (Conus species), which are marine mollusks that can deliver venomous stings.

It is crucial to emphasize that while encounters with these deadly animals can and do occur, the likelihood of such encounters is generally quite low. It is important for residents and visitors to Australia to be aware of their surroundings, follow safety protocols, and seek professional assistance in case of any encounters with dangerous wildlife."""
    query = "What arte the most dangerous animals in Australia?"

    query_sentences = Page.split_into_sentences(None, query)
    gpt_sentences = Page.split_into_sentences(None, gpt_response)
    print(query_sentences)
    print(gpt_sentences)
    
